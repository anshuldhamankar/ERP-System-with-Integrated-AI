import os
import chromadb
from chromadb import Documents, EmbeddingFunction, Embeddings
from datetime import datetime

CHROMA_PATH = "chroma_db"
COLLECTION_NAME = "erp_knowledge"


class GeminiEmbeddingFunction(EmbeddingFunction):
    """Custom embedding function using google-genai client."""
    def __call__(self, input: Documents) -> Embeddings:
        from google import genai
        client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))
        result = client.models.embed_content(
            model="gemini-embedding-001",
            contents=input
        )
        return [e.values for e in result.embeddings]


def get_chroma_collection():
    client = chromadb.PersistentClient(path=CHROMA_PATH)
    collection = client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=GeminiEmbeddingFunction(),
        metadata={"hnsw:space": "cosine"}
    )
    return collection


def seed_static_knowledge():
    """Seed one-time static ERP knowledge chunks. Skips if already seeded."""
    collection = get_chroma_collection()
    if collection.count() > 0:
        return

    chunks = [
        ("erp_001", "This ERP system is used by DigiMart shop. It manages stock, billing, suppliers, customers, ledger and reports."),
        ("erp_002", "Products have a name, SKU, stock quantity, unit price (GST inclusive selling price), and company/brand. Stock is added via Add Stock page."),
        ("erp_003", "Invoices are tax invoices with GST. Unit price entered is GST inclusive. CGST and SGST are back-calculated. Net profit per invoice = grand total (sell price incl GST) minus total cost price (purchase price incl GST)."),
        ("erp_004", "Suppliers are parties of type supplier. When stock is purchased on credit, a ledger entry credits the supplier. Supplier payment debits the supplier and credits cash."),
        ("erp_005", "Customers are parties of type customer. Credit sales debit the customer in ledger. Cash receipts credit the customer and debit cash. Max cash receipt per voucher is Rs 10000."),
        ("erp_006", "The ledger shows all journal entries. Each transaction posts a double entry. Party ledger shows only entries where account_title matches the party name."),
        ("erp_007", "Reports show total inventory value, low stock products (below 10 units), net profit from invoices, sales revenue over time, top selling products."),
        ("erp_008", "DM Memo is a delivery/dispatch memo. It records items dispatched to a party with batch, quantity and transport details."),
        ("erp_009", "Low stock alert triggers when product stock_quantity is less than 10 units. Dashboard shows these products."),
        ("erp_010", "GST rates supported: 0%, 5%, 12%, 18%, 28%. CGST = SGST = half of total GST. Taxable amount = inclusive price / (1 + GST rate/100)."),
    ]

    collection.add(
        ids=[c[0] for c in chunks],
        documents=[c[1] for c in chunks]
    )
    print(f"Seeded {len(chunks)} static knowledge chunks into ChromaDB.")


def _doc_exists(collection, doc_id):
    try:
        return len(collection.get(ids=[doc_id])['ids']) > 0
    except Exception:
        return False


def index_live_data(db):
    """Re-index live business data into ChromaDB."""
    collection = get_chroma_collection()
    docs, ids = [], []

    # Low stock
    low = db.execute('SELECT name, stock_quantity, unit_price FROM Product WHERE stock_quantity < 10').fetchall()
    if low:
        docs.append("Low stock products: " + ", ".join(
            f"{r['name']} (qty: {r['stock_quantity']}, price: Rs{r['unit_price']})" for r in low))
        ids.append("live_low_stock")

    # Top 5 products by sales
    top = db.execute('''
        SELECT p.name, SUM(t.quantity) as sold
        FROM "Transaction" t JOIN Product p ON t.product_id=p.id
        WHERE t.type='sale' GROUP BY p.name ORDER BY sold DESC LIMIT 5
    ''').fetchall()
    if top:
        docs.append("Top selling products: " + ", ".join(f"{r['name']} ({r['sold']} units sold)" for r in top))
        ids.append("live_top_products")

    # Revenue and profit
    rev = db.execute('SELECT COALESCE(SUM(grand_total),0) as r, COALESCE(SUM(net_profit),0) as p FROM Invoice').fetchone()
    docs.append(f"Total sales revenue: Rs{round(rev['r'],2)}. Total net profit: Rs{round(rev['p'],2)}.")
    ids.append("live_revenue")

    # Supplier outstanding balances
    sups = db.execute('''
        SELECT p.name,
               COALESCE(SUM(CASE WHEN le.account_title=p.name THEN le.credit ELSE 0 END),0)
               - COALESCE(SUM(CASE WHEN le.account_title=p.name THEN le.debit ELSE 0 END),0) AS balance
        FROM Party p LEFT JOIN LedgerEntry le ON le.party_id=p.id
        WHERE p.type='supplier' GROUP BY p.id HAVING balance > 0
    ''').fetchall()
    if sups:
        docs.append("Outstanding supplier balances: " + ", ".join(f"{r['name']}: Rs{round(r['balance'],2)}" for r in sups))
        ids.append("live_supplier_balances")

    # Customer outstanding balances
    custs = db.execute('''
        SELECT p.name,
               COALESCE(SUM(CASE WHEN le.account_title=p.name THEN le.debit ELSE 0 END),0)
               - COALESCE(SUM(CASE WHEN le.account_title=p.name THEN le.credit ELSE 0 END),0) AS balance
        FROM Party p LEFT JOIN LedgerEntry le ON le.party_id=p.id
        WHERE p.type='customer' GROUP BY p.id HAVING balance > 0
    ''').fetchall()
    if custs:
        docs.append("Customers with pending dues: " + ", ".join(f"{r['name']}: Rs{round(r['balance'],2)}" for r in custs))
        ids.append("live_customer_dues")

    # Recent invoices
    recent = db.execute('''
        SELECT invoice_number, invoice_date, bill_to_name, grand_total, net_profit
        FROM Invoice ORDER BY id DESC LIMIT 5
    ''').fetchall()
    if recent:
        docs.append("Recent invoices: " + ", ".join(
            f"#{r['invoice_number']} on {r['invoice_date']} to {r['bill_to_name']} Rs{r['grand_total']} (profit Rs{r['net_profit']})"
            for r in recent))
        ids.append("live_recent_invoices")

    # Stock summary
    total_val = db.execute('SELECT COALESCE(SUM(stock_quantity*unit_price),0) FROM Product').fetchone()[0]
    total_prod = db.execute('SELECT COUNT(*) FROM Product').fetchone()[0]
    docs.append(f"Total products: {total_prod}. Total inventory value: Rs{round(total_val,2)}.")
    ids.append("live_stock_summary")

    # Upsert: delete existing live_ docs then re-add
    existing = [i for i in ids if _doc_exists(collection, i)]
    if existing:
        collection.delete(ids=existing)
    if docs:
        collection.add(ids=ids, documents=docs)


def rag_answer(question, db):
    from google import genai
    collection = get_chroma_collection()

    # Retrieve top 5 relevant chunks
    n = min(5, collection.count())
    if n == 0:
        context = "No knowledge base context available."
    else:
        results = collection.query(query_texts=[question], n_results=n)
        chunks = results['documents'][0] if results['documents'] else []
        context = "\n".join(f"- {c}" for c in chunks)

    # Live snapshot
    low_stock = db.execute(
        'SELECT name, stock_quantity FROM Product WHERE stock_quantity < 10 ORDER BY stock_quantity ASC LIMIT 5'
    ).fetchall()
    total_profit  = db.execute('SELECT COALESCE(SUM(net_profit),0) FROM Invoice').fetchone()[0]
    total_revenue = db.execute('SELECT COALESCE(SUM(grand_total),0) FROM Invoice').fetchone()[0]
    credit_invoices = db.execute("SELECT COUNT(*) FROM Invoice WHERE bill_type='credit'").fetchone()[0]

    live = f"""Current live data:
- Low stock items: {[dict(r) for r in low_stock] or 'None'}
- Total revenue: Rs{round(total_revenue, 2)}
- Total net profit: Rs{round(total_profit, 2)}
- Pending credit invoices: {credit_invoices}
- Today: {datetime.now().strftime('%d %B %Y')}"""

    prompt = f"""You are an intelligent ERP assistant for DigiMart shop.
Answer the user's question using the knowledge base context and live data below.
Be concise, specific, and use Rs for currency. If you don't know, say so.

Knowledge base context:
{context}

{live}

User question: {question}

Answer:"""

    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY", ""))
    response = client.models.generate_content(
        model="models/gemini-3.5-flash-lite",
        contents=prompt
    )
    return response.text.strip()
