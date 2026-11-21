import sqlite3
from flask import Flask,render_template, request,redirect,url_for,flash, g,jsonify
from datetime import datetime, timedelta
import os
import json
from google import genai
from ai_service import seed_static_knowledge, index_live_data, rag_answer

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'ERP-dev-key')

# --- DB Configuration ---

DATABASE = 'database.db'

"""connects to existing db  / Cretes new"""
def get_db():
    db=getattr(g,'_database',None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE)
        db.row_factory = sqlite3.Row
    return db

"""disconnects from the db at the end of the request"""
@app.teardown_appcontext
def close_connection(exception):
    db =getattr(g,'_database', None)
    if db is not None:
        db.close()

"""initialize the db schema."""
def init_db():
    with app.app_context():
        db =get_db()
        cursor=db.cursor()
        # create supplier table if it does not exist
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Supplier (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                contact_email TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # create product table if it does not exist
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Product (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                sku TEXT NOT NULL UNIQUE,
                stock_quantity INTEGER NOT NULL,
                unit_price REAL NOT NULL,
                supplier_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (supplier_id) REFERENCES Supplier (id) ON DELETE SET NULL
            )
        ''')
        # create transaction table if not exists
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS "Transaction" (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('purchase', 'sale')),
                date TEXT NOT NULL, -- Stored as replete-MM-DD
                unit_price REAL NOT NULL,
                FOREIGN KEY (product_id) REFERENCES Product (id) ON DELETE CASCADE
            )
        ''')
        # Invoice table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Invoice (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_number TEXT NOT NULL UNIQUE,
                invoice_date TEXT NOT NULL,
                due_date TEXT NOT NULL,
                supplier_id INTEGER,
                bill_to_name TEXT NOT NULL,
                bill_to_gstin TEXT,
                bill_to_pos TEXT,
                ship_to_name TEXT,
                ship_to_address TEXT,
                ship_to_mobile TEXT,
                ship_to_email TEXT,
                terms TEXT,
                cgst_rate REAL DEFAULT 0,
                sgst_rate REAL DEFAULT 0,
                cgst2_rate REAL DEFAULT 0,
                sgst2_rate REAL DEFAULT 0,
                round_off REAL DEFAULT 0,
                status TEXT DEFAULT 'draft' CHECK(status IN ('draft','sent','paid')),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (supplier_id) REFERENCES Supplier (id) ON DELETE SET NULL
            )
        ''')
        # Invoice line items
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS InvoiceItem (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id INTEGER NOT NULL,
                product_id INTEGER,
                item_description TEXT NOT NULL,
                batch TEXT,
                mfg_date TEXT,
                exp_date TEXT,
                manufacturer TEXT,
                quantity INTEGER NOT NULL,
                rate REAL NOT NULL,
                tax_label TEXT,
                tax_rate REAL DEFAULT 0,
                FOREIGN KEY (invoice_id) REFERENCES Invoice (id) ON DELETE CASCADE,
                FOREIGN KEY (product_id) REFERENCES Product (id) ON DELETE SET NULL
            )
        ''')
        # General Journal / Ledger entries
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS LedgerEntry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_date TEXT NOT NULL,
                account_title TEXT NOT NULL,
                explanation TEXT,
                ref TEXT,
                debit REAL DEFAULT 0,
                credit REAL DEFAULT 0,
                group_id INTEGER,
                party_id INTEGER,
                invoice_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Party (customers & suppliers for ledger accounts)
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS Party ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "name TEXT NOT NULL UNIQUE,"
            "type TEXT NOT NULL CHECK(type IN ('customer','supplier')),"
            "phone TEXT,"
            "email TEXT,"
            "address TEXT,"
            "gstin TEXT,"
            "opening_balance REAL DEFAULT 0,"
            "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        # Shop settings (single row)
        cursor.execute(
            "CREATE TABLE IF NOT EXISTS ShopSettings ("
            "id INTEGER PRIMARY KEY CHECK(id=1),"
            "shop_name TEXT DEFAULT 'My Shop',"
            "owner_name TEXT,"
            "address TEXT,"
            "phone TEXT,"
            "email TEXT,"
            "gstin TEXT,"
            "default_cgst REAL DEFAULT 9,"
            "default_sgst REAL DEFAULT 9,"
            "terms TEXT DEFAULT 'Goods once sold will not be taken back or exchange.',"
            "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        # Seed default settings row
        cursor.execute('''
            INSERT OR IGNORE INTO ShopSettings (id) VALUES (1)
        ''')
        # DMMemo table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS DMMemo (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dm_number TEXT NOT NULL UNIQUE,
                dm_date TEXT NOT NULL,
                party_name TEXT NOT NULL,
                party_mobile TEXT,
                party_address TEXT,
                transport TEXT,
                ref_invoice TEXT,
                remarks TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS DMMemoItem (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dm_id INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                company TEXT,
                batch TEXT,
                quantity INTEGER NOT NULL,
                unit TEXT DEFAULT 'Nos',
                FOREIGN KEY (dm_id) REFERENCES DMMemo(id) ON DELETE CASCADE
            )
        ''')
        # CashReceipt table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS CashReceipt (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                party_id INTEGER NOT NULL,
                payment_date TEXT NOT NULL,
                amount REAL NOT NULL,
                receipt_ref TEXT NOT NULL,
                ledger_group_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (party_id) REFERENCES Party(id)
            )
        ''')
        # SupplierPayment table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS SupplierPayment (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                party_id INTEGER NOT NULL,
                payment_date TEXT NOT NULL,
                amount REAL NOT NULL,
                receipt_ref TEXT NOT NULL,
                payment_mode TEXT DEFAULT 'Cash',
                notes TEXT,
                ledger_group_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (party_id) REFERENCES Party(id)
            )
        ''')
        # Add new columns to Invoice if upgrading existing db
        try:
            cursor.execute("ALTER TABLE LedgerEntry ADD COLUMN invoice_id INTEGER")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE LedgerEntry ADD COLUMN party_id INTEGER")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE InvoiceItem ADD COLUMN cost_price REAL DEFAULT 0")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE Product ADD COLUMN company TEXT")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE Invoice ADD COLUMN bill_type TEXT DEFAULT 'cash'")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE Invoice ADD COLUMN party_id INTEGER")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE Invoice ADD COLUMN total_taxable REAL DEFAULT 0")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE Invoice ADD COLUMN total_cgst REAL DEFAULT 0")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE Invoice ADD COLUMN total_sgst REAL DEFAULT 0")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE Invoice ADD COLUMN grand_total REAL DEFAULT 0")
        except Exception:
            pass
        try:
            cursor.execute("ALTER TABLE Invoice ADD COLUMN net_profit REAL DEFAULT 0")
        except Exception:
            pass
        # One-time cleanup: unlink contra entries wrongly linked to party_id
        # Only keep entries where account_title matches the party name
        db.execute('''
            UPDATE LedgerEntry SET party_id = NULL
            WHERE party_id IS NOT NULL
            AND account_title NOT IN (SELECT name FROM Party)
        ''')
        db.commit()
        print("Database initialized successfully.")


# --- Helper Functions for Database Operations ---
# These must be defined BEFORE the routes that use them

"""fetches all products with their supplier names and converts date strings to datetime objects."""
def get_all_products():

    db =get_db()
    products_raw= db.execute('''
        SELECT p.*, s.name AS supplier_name
        FROM Product p
        LEFT JOIN Supplier s ON p.supplier_id = s.id
        ORDER BY p.name
    ''').fetchall()

    # convert 'created_at' and 'updated_at' strings to datetime tyope
    products_list= []
    for product in products_raw:
        product_dict= dict(product)
        if product_dict['created_at']:
            # SQLite stores TIMESTAMP as 'YYYY-MM-DD HH:MM:%S'
            product_dict['created_at'] =datetime.strptime(product_dict['created_at'],'%Y-%m-%d %H:%M:%S')
        if product_dict['updated_at']:
            product_dict['updated_at']=datetime.strptime(product_dict['updated_at'],'%Y-%m-%d %H:%M:%S')
        products_list.append(product_dict)
    return products_list

"""load a single product by ID."""
def get_product_by_id(product_id):
    db= get_db()
    product =db.execute('SELECT * FROM Product WHERE id = ?',(product_id,)).fetchone()
    # if a product exists... convert its date strings to datetime objects
    if product:
        product_dict= dict(product)
        if product_dict['created_at']:
            product_dict['created_at'] =datetime.strptime(product_dict['created_at'], '%Y-%m-%d %H:%M:%S')
        if product_dict['updated_at']:
            product_dict['updated_at']=datetime.strptime(product_dict['updated_at'],'%Y-%m-%d %H:%M:%S')
        return product_dict
    return None


"""fetch all suppliers and counts products associated with each."""
def get_all_suppliers():
    db=get_db()
    suppliers= db.execute('''
        SELECT s.*, COUNT(p.id) AS product_count
        FROM Supplier s
        LEFT JOIN Product p ON s.id = p.supplier_id
        GROUP BY s.id
        ORDER BY s.name
    ''').fetchall()
    return suppliers


"""fetches a single supplier by ID."""
def get_supplier_by_id(supplier_id):
    db=get_db()
    supplier=db.execute('SELECT * FROM Supplier WHERE id = ?',(supplier_id,)).fetchone()
    return supplier

"""fetche all transactions with product names and converts date strings to datetime objects"""
def get_all_transactions():
    db = get_db()
    transactions_raw = db.execute('''
        SELECT t.*, p.name AS product_name, p.sku AS product_sku, p.unit_price AS product_current_price
        FROM "Transaction" t
        JOIN Product p ON t.product_id = p.id
        ORDER BY t.date DESC, t.id DESC
    ''').fetchall()

    transactions_list = []
    for txn in transactions_raw:
        txn_dict= dict(txn)
        if txn_dict['date']:
            txn_dict['date']=datetime.strptime(txn_dict['date'],'%Y-%m-%d')
        transactions_list.append (txn_dict)
    return transactions_list

"""fetches a single transaction by ID...including product details."""
def get_transaction_by_id(transaction_id):
    db =get_db()
    txn=db.execute('''
        SELECT t.*, p.name AS product_name, p.sku AS product_sku, p.stock_quantity AS current_product_stock
        FROM "Transaction" t
        JOIN Product p ON t.product_id = p.id
        WHERE t.id = ?
    ''', (transaction_id,)).fetchone()
    if txn:
        txn_dict =dict(txn)
        if txn_dict['date']:
            txn_dict['date']=datetime.strptime(txn_dict['date'],'%Y-%m-%d')
        return txn_dict
    return None


def suggest_reorder_quantity_from_ai(product_id):
    db = get_db()
    product = get_product_by_id(product_id)

    if not product:
        return {"error": "Product not found."}, 404

    thirty_days_ago = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d')
    transactions_30_days_raw = db.execute('''
        SELECT type, SUM(quantity) as total_quantity
        FROM "Transaction"
        WHERE product_id = ? AND date >= ?
        GROUP BY type
    ''', (product_id, thirty_days_ago)).fetchall()

    total_sales_30_days = 0
    total_purchases_30_days = 0
    for txn in transactions_30_days_raw:
        if txn['type'] == 'sale':
            total_sales_30_days += txn['total_quantity']
        elif txn['type'] == 'purchase':
            total_purchases_30_days += txn['total_quantity']

    prompt_text = (
        f"Suggest an optimal reorder quantity for {product['name']} (SKU: {product['sku']}). "
        f"Current stock: {product['stock_quantity']} units. "
        f"Transaction history for the last 30 days: {total_sales_30_days} units sold, "
        f"{total_purchases_30_days} units purchased. "
        "Consider a typical lead time of 7 days and aim to maintain stock for approximately 30 days of sales. "
        "Output your suggestion ONLY as a valid JSON object with exactly two keys: "
        "'reorder_quantity' (integer) and 'reasoning' (string). No extra text outside the JSON."
    )

    try:
        client = genai.Client(api_key=os.environ.get('GEMINI_API_KEY', ''))
        response = client.models.generate_content(
            model='models/gemini-3.5-flash-lite',
            contents=prompt_text
        )
        ai_response_content = response.text.strip()
        print(f"DEBUG: Gemini Response: '{ai_response_content}'")

        if not ai_response_content:
            return {"error": "Gemini returned an empty response."}, 500

        if ai_response_content.startswith('```'):
            ai_response_content = ai_response_content.split('```')[1]
            if ai_response_content.startswith('json'):
                ai_response_content = ai_response_content[4:]
            ai_response_content = ai_response_content.strip()

        ai_data = json.loads(ai_response_content)
        reorder_qty = ai_data.get('reorder_quantity')
        reasoning   = ai_data.get('reasoning', 'No specific reasoning provided.')

        if isinstance(reorder_qty, int) and reorder_qty >= 0:
            return {"reorder_quantity": reorder_qty, "reasoning": reasoning}, 200
        else:
            return {"error": "Gemini returned an invalid reorder quantity format or value."}, 500

    except json.JSONDecodeError as e:
        print(f"ERROR: JSON Decode Error: {e} — raw: '{ai_response_content}'")
        return {"error": f"Failed to parse Gemini response as JSON: {e}"}, 500
    except Exception as e:
        print(f"An error occurred during Gemini AI integration: {e}")
        return {"error": f"Failed to get AI suggestion from Gemini: {e}"}, 500


# --- Routes ---

@app.route('/')
@app.route('/dashboard')
def dashboard():
    """if available stock of x product < 10 then add x in low_stoc_products."""
    db=get_db()

    total_products=db.execute('SELECT COUNT(*) FROM Product').fetchone()[0]

    total_value_row=db.execute('SELECT SUM(stock_quantity * unit_price) FROM Product').fetchone()
    total_value= round(total_value_row[0] if total_value_row[0] is not None else 0, 2)

    low_stock_products =db.execute('''
        SELECT p.*, s.name AS supplier_name
        FROM Product p
        LEFT JOIN Supplier s ON p.supplier_id = s.id
        WHERE p.stock_quantity < 10
        ORDER BY p.stock_quantity ASC
    ''').fetchall()
    low_stock_count= len(low_stock_products)

    total_transactions=db.execute('SELECT COUNT(*) FROM "Transaction"').fetchone()[0]

    return render_template('dashboard.html',
                           total_products=total_products,
                           total_value=total_value,
                           low_stock_products=low_stock_products,
                           low_stock_count=low_stock_count,
                           total_transactions=total_transactions)

@app.route('/products')
def products():
    """display the product list and a form for adding products"""
    products_list=get_all_products()
    suppliers_list=get_all_suppliers()
    return render_template('products.html', products=products_list,suppliers=suppliers_list)

@app.route('/add_product',methods=['POST'])
def add_product():
    """logic for adding a product."""
    name=request.form['name']
    sku=request.form['sku']
    try:
        unit_price=float(request.form['unit_price'])
        stock_quantity=int(request.form['stock_quantity'])
        supplier_id=int(request.form['supplier_id'])
    except ValueError:
        flash('Invalid input for price, quantity, or supplier. Please ensure they are numbers.', 'danger')
        return redirect(url_for('products'))

    import math
    if math.isnan(unit_price) or unit_price <= 0 or stock_quantity < 0:
        flash('Price must be positive and stock quantity cannot be negative.', 'danger')
        return redirect(url_for('products'))

    db=get_db()
    cursor=db.cursor()
    try:
        cursor.execute('''
            INSERT INTO Product (name, sku, stock_quantity, unit_price, supplier_id)
            VALUES (?, ?, ?, ?, ?)
        ''', (name, sku, stock_quantity, unit_price, supplier_id))
        db.commit()
        flash('Product added successfully!', 'success')
    except sqlite3.IntegrityError:
        flash(f'Error: Product with SKU "{sku}" already exists or supplier does not exist.', 'danger')
    except Exception as e:
        flash(f'An error occurred: {e}', 'danger')
    return redirect(url_for('products'))

@app.route('/edit_product/<int:product_id>', methods=['GET', 'POST'])
def edit_product(product_id):
    """loads the edit product page and handles product updates."""
    db= get_db()
    product=get_product_by_id(product_id)
    suppliers_list= get_all_suppliers()

    if not product:
        flash('Product not found.', 'danger')
        return redirect(url_for('products'))

    if request.method=='POST':
        name=request.form['name']
        sku= request.form['sku']
        try:
            unit_price= float(request.form['unit_price'])
            stock_quantity= int(request.form['stock_quantity'])
            supplier_id =int(request.form['supplier_id'])
        except ValueError:
            flash('Invalid input for price, quantity, or supplier. Please ensure they are numbers.', 'danger')
            return redirect(url_for('edit_product',product_id=product_id))

        import math
        if math.isnan(unit_price) or unit_price <= 0 or stock_quantity < 0:
            flash('Price must be positive and stock quantity cannot be negative.', 'danger')
            return redirect(url_for('edit_product',product_id=product_id))

        try:
            db.execute('''
                UPDATE Product
                SET name = ?, sku = ?, stock_quantity = ?, unit_price = ?, supplier_id = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (name, sku, stock_quantity, unit_price, supplier_id, product_id))
            db.commit()
            flash('Product updated successfully!', 'success')
            return redirect(url_for('products'))
        except sqlite3.IntegrityError:
            flash(f'Error: Product with SKU "{sku}" already exists or supplier does not exist.', 'danger')
        except Exception as e:
            flash(f'An error occurred: {e}','danger')

    return render_template('edit_product.html', product=product, suppliers=suppliers_list)


@app.route('/delete_product/<int:product_id>',methods=['POST'])
def delete_product(product_id):
    """handles deleting a product from the db. Transactions are cascade-deleted by the DB schema."""
    db = get_db()
    try:
        db.execute('DELETE FROM Product WHERE id = ?',(product_id,))
        db.commit()
        flash('Product deleted successfully!', 'success')
    except Exception as e:
        flash(f'An error occurred while deleting the product: {e}','danger')
    return redirect(url_for('products'))

@app.route('/suppliers')
def suppliers():
    """loads the suppliers page, show all suppliers and add form"""
    suppliers_list=get_all_suppliers()
    return render_template('suppliers.html', suppliers=suppliers_list)

@app.route('/add_supplier', methods=['POST'])
def add_supplier():
    """logic for adding a new supplier to the database."""
    name=request.form['name']
    contact_email=request.form.get('contact_email')

    db=get_db()
    cursor=db.cursor()
    try:
        cursor.execute('''
            INSERT INTO Supplier (name, contact_email)
            VALUES (?, ?)
        ''', (name,contact_email))
        db.commit()
        flash('Supplier added successfully!', 'success')
    except sqlite3.IntegrityError:
        flash(f'Error: Supplier with name "{name}" already exists.', 'danger')
    except Exception as e:
        flash(f'An error occurred: {e}', 'danger')
    return redirect(url_for('suppliers'))


@app.route('/edit_supplier/<int:supplier_id>', methods=['GET', 'POST'])
def edit_supplier(supplier_id):
    """loads the edit supplier page and handles supplier updates."""
    supplier = get_supplier_by_id(supplier_id)
    if not supplier:
        flash('Supplier not found.', 'danger')
        return redirect(url_for('suppliers'))

    if request.method == 'POST':
        name = request.form['name'].strip()
        contact_email = request.form.get('contact_email', '').strip() or None
        if not name:
            flash('Supplier name cannot be empty.', 'danger')
            return redirect(url_for('edit_supplier', supplier_id=supplier_id))
        db = get_db()
        try:
            db.execute('''
                UPDATE Supplier SET name = ?, contact_email = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (name, contact_email, supplier_id))
            db.commit()
            flash('Supplier updated successfully!', 'success')
            return redirect(url_for('suppliers'))
        except sqlite3.IntegrityError:
            flash(f'Error: Supplier with name "{name}" already exists.', 'danger')
        except Exception as e:
            flash(f'An error occurred: {e}', 'danger')

    return render_template('edit_supplier.html', supplier=supplier)


@app.route('/delete_supplier/<int:supplier_id>', methods=['POST'])
def delete_supplier(supplier_id):
    """logic for deleting a supplier from the db only if no products are associated."""
    db=get_db()
    try:
        product_count=db.execute('SELECT COUNT(*) FROM Product WHERE supplier_id = ?',(supplier_id,)).fetchone()[0]
        if product_count > 0:
            flash('Cannot delete supplier. Products are associated with this supplier.','danger')
        else:
            db.execute('DELETE FROM Supplier WHERE id = ?', (supplier_id,))
            db.commit()
            flash('Supplier deleted successfully!', 'success')
    except Exception as e:
        flash(f'An error occurred while deleting the supplier: {e}', 'danger')
    return redirect(url_for('suppliers'))

@app.route('/transactions')
def transactions():
    """loads the transactions page... showing transaction history and a form to add new transaction"""
    transactions_list =get_all_transactions()
    products_list=get_all_products()
    current_date=datetime.now().strftime('%Y-%m-%d')
    return render_template('transactions.html',
                           transactions=transactions_list,
                           products=products_list,
                           current_date=current_date)

@app.route('/add_transaction',methods=['POST'])
def add_transaction():
    """logic for adding a new transaction and updating product stock"""
    product_id=request.form['product_id']
    transaction_type = request.form['type']
    date =request.form['date']
    try:
        quantity=int(request.form['quantity'])
        unit_price=float(request.form['unit_price'])
    except ValueError:
        flash('Invalid input for quantity or price. Please ensure they are numbers.', 'danger')
        return redirect(url_for('transactions'))

    import math
    if quantity <= 0 or math.isnan(unit_price) or unit_price <= 0:
        flash('Quantity and Unit Price must be positive.', 'danger')
        return redirect(url_for('transactions'))

    db=get_db()
    cursor=db.cursor()
    product=get_product_by_id(product_id)

    if not product:
        flash('Selected product not found.', 'danger')
        return redirect(url_for('transactions'))

    new_stock=product['stock_quantity']

    try:
        if transaction_type=='sale':
            if new_stock < quantity:
                flash(f'Not enough stock for sale! Only {new_stock} units available for {product["name"]}.', 'danger')
                return redirect(url_for('transactions'))
            new_stock-=quantity
        elif transaction_type == 'purchase':
            new_stock = quantity+ new_stock
        else:
            flash('Invalid transaction type.', 'danger')
            return redirect(url_for('transactions'))

        cursor.execute('''
            INSERT INTO "Transaction" (product_id, quantity, type, date, unit_price)
            VALUES (?, ?, ?, ?, ?)
        ''', (product_id,quantity,transaction_type,date,unit_price))

        cursor.execute('''
            UPDATE Product
            SET stock_quantity = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (new_stock,product_id))

        # Auto-post purchase to supplier party ledger
        if transaction_type == 'purchase':
            supplier = db.execute(
                'SELECT s.name FROM Supplier s JOIN Product p ON p.supplier_id=s.id WHERE p.id=?',
                (product_id,)).fetchone()
            if supplier:
                sup_name = supplier['name']
                sup_party_id = get_or_create_party(db, sup_name, 'supplier')
                total_amt = round(quantity * unit_price, 2)
                last_group = db.execute('SELECT MAX(group_id) FROM LedgerEntry').fetchone()[0]
                grp = (last_group or 0) + 1
                ref = f'PUR-{product["sku"]}-{date}'
                # Dr Purchases (no party) / Cr Supplier (party link only)
                cursor.execute('''
                    INSERT INTO LedgerEntry (entry_date,account_title,explanation,ref,debit,credit,group_id)
                    VALUES (?,?,?,?,?,?,?)
                ''', (date,'Purchases Account',f'Purchase - {product["name"]}',ref,total_amt,0,grp))
                cursor.execute('''
                    INSERT INTO LedgerEntry (entry_date,account_title,explanation,ref,debit,credit,group_id,party_id)
                    VALUES (?,?,?,?,?,?,?,?)
                ''', (date,sup_name,f'Purchase - {product["name"]}',ref,0,total_amt,grp,sup_party_id))

        db.commit()
        flash('Transaction added and stock updated successfully!', 'success')
    except Exception as e:
        db.rollback()
        flash(f'An error occurred during transaction: {e}', 'danger')
    return redirect(url_for('transactions'))


@app.route('/edit_transaction/<int:transaction_id>', methods=['GET', 'POST'])
def edit_transaction(transaction_id):
    """loads the edit transaction page and handles transaction updates"""
    db=get_db()
    transaction=get_transaction_by_id(transaction_id)
    products_list=get_all_products()

    if not transaction:
        flash('Transaction not found.', 'danger')
        return redirect(url_for('transactions'))

    if request.method=='POST':
        new_product_id= int(request.form['product_id'])
        new_type =request.form['type']
        new_date_str =request.form['date']
        try:
            new_quantity=int(request.form['quantity'])
            new_unit_price=float(request.form['unit_price'])
        except ValueError:
            flash('Invalid input for quantity or price. Please ensure they are numbers.', 'danger')
            return redirect(url_for('edit_transaction',transaction_id=transaction_id))

        import math
        if new_quantity <= 0 or math.isnan(new_unit_price) or new_unit_price <= 0:
            flash('Quantity and Unit Price must be positive.', 'danger')
            return redirect(url_for('edit_transaction',transaction_id=transaction_id))

        original_txn = get_transaction_by_id(transaction_id)
        original_product = get_product_by_id(original_txn['product_id'])

        if not original_product:
            flash('Original product for this transaction not found!', 'danger')
            return redirect(url_for('transactions'))

        try:
            db_conn = get_db()
            cursor = db_conn.cursor()

            # Step 1: revert the original transaction's effect on the original product
            reverted_original_stock = original_product['stock_quantity']
            if original_txn['type'] == 'sale':
                reverted_original_stock += original_txn['quantity']
            elif original_txn['type'] == 'purchase':
                reverted_original_stock -= original_txn['quantity']

            # Step 2: determine the starting stock for the target product
            if original_txn['product_id'] != new_product_id:
                # Different product: write reverted stock back to original, read new product's actual stock
                target_product = get_product_by_id(new_product_id)
                if not target_product:
                    flash('New selected product not found!', 'danger')
                    return redirect(url_for('edit_transaction', transaction_id=transaction_id))
                cursor.execute('UPDATE Product SET stock_quantity = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                               (reverted_original_stock, original_txn['product_id']))
                current_target_stock = target_product['stock_quantity']
            else:
                # Same product: target stock is the reverted stock
                target_product = original_product
                current_target_stock = reverted_original_stock

            # Step 3: apply the new transaction to the target product
            if new_type == 'sale':
                if current_target_stock < new_quantity:
                    flash(f'Not enough stock for sale! Only {current_target_stock} units available for {target_product["name"]}.', 'danger')
                    db_conn.rollback()
                    return redirect(url_for('edit_transaction', transaction_id=transaction_id))
                current_target_stock -= new_quantity
            elif new_type == 'purchase':
                current_target_stock += new_quantity

            cursor.execute('''
                UPDATE Product SET stock_quantity = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?
            ''', (current_target_stock, new_product_id))

            cursor.execute('''
                UPDATE "Transaction"
                SET product_id = ?, quantity = ?, type = ?, date = ?, unit_price = ?
                WHERE id = ?
            ''', (new_product_id, new_quantity, new_type, new_date_str, new_unit_price, transaction_id))
            db_conn.commit()
            flash('Transaction updated successfully!', 'success')
            return redirect(url_for('transactions'))
        except Exception as e:
            db_conn.rollback()
            flash(f'An error occurred during transaction update: {e}', 'danger')

    return render_template('edit_transaction.html',
                           transaction=transaction,
                           products=products_list,
                           current_date=transaction['date'].strftime('%Y-%m-%d'))


@app.route('/delete_transaction/<int:transaction_id>', methods=['POST'])
def delete_transaction(transaction_id):
    """loads deleting a transaction and reverting product stoc"""
    db=get_db()
    cursor=db.cursor()
    transaction =get_transaction_by_id(transaction_id)

    if not transaction:
        flash('Transaction not found.', 'danger')
        return redirect(url_for('transactions'))

    product_id= transaction['product_id']
    quantity=transaction['quantity']
    transaction_type=transaction['type']

    try:
        # get current product stock
        product=get_product_by_id(product_id)
        if not product:
            flash('Associated product not found for this transaction.', 'danger')
            return redirect(url_for('transactions'))

        new_stock=product['stock_quantity']

        #revert stock change
        if transaction_type == 'sale':
            new_stock += quantity
        elif transaction_type == 'purchase':
            # ensure stock doesnt go negative if a purchase that made it positive is deleted
            if new_stock-quantity< 0:
                 flash(f'Cannot delete purchase transaction. Stock for {product["name"]} would become negative.', 'danger')
                 return redirect(url_for('transactions'))
            new_stock-=quantity

        #update product stock
        cursor.execute('''
            UPDATE Product
            SET stock_quantity = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (new_stock,product_id))

        #delete the transaction
        cursor.execute('DELETE FROM "Transaction" WHERE id = ?', (transaction_id,))
        db.commit()
        flash('Transaction deleted and stock reverted successfully!', 'success')
    except Exception as e:
        db.rollback()
        flash(f'An error occurred while deleting transaction: {e}', 'danger')
    return redirect(url_for('transactions'))


@app.route('/reports')
def reports():
    db=get_db()

    total_value_row=db.execute('SELECT SUM(stock_quantity * unit_price) FROM Product').fetchone()
    total_value=round(total_value_row[0] if total_value_row[0] is not None else 0, 2)

    low_stock_products_raw=db.execute('''
        SELECT p.id, p.name, p.stock_quantity, s.name AS supplier_name
        FROM Product p
        LEFT JOIN Supplier s ON p.supplier_id = s.id
        WHERE p.stock_quantity < 10
        ORDER BY p.stock_quantity ASC
    ''').fetchall()
    low_stock_count=len(low_stock_products_raw)
    supplier_count=db.execute('SELECT COUNT(*) FROM Supplier').fetchone()[0]

    # Revenue and profit grouped by month from invoices
    sales_data_raw = db.execute('''
        SELECT strftime('%Y-%m', invoice_date) as month,
               SUM(grand_total) as total_sales,
               SUM(net_profit) as total_profit
        FROM Invoice
        GROUP BY month ORDER BY month DESC LIMIT 6
    ''').fetchall()
    sales_months = [row['month'] for row in reversed(sales_data_raw)]
    sales_values = [round(row['total_sales'] or 0, 2) for row in reversed(sales_data_raw)]

    net_profit_months = sales_months
    net_profit_values = [round(row['total_profit'] or 0, 2) for row in reversed(sales_data_raw)]

    net_sales_months = sales_months
    net_sales_values = sales_values

    total_net_profit = round(db.execute('SELECT COALESCE(SUM(net_profit),0) FROM Invoice').fetchone()[0], 2)

    top_products_raw=db.execute('''
        SELECT p.name, SUM(t.quantity) AS total_sold_quantity
        FROM "Transaction" t JOIN Product p ON t.product_id = p.id
        WHERE t.type = 'sale'
        GROUP BY p.name ORDER BY total_sold_quantity DESC LIMIT 5
    ''').fetchall()
    top_products_names=[row['name'] for row in top_products_raw]
    top_products_counts=[row['total_sold_quantity'] for row in top_products_raw]

    purchase_count=db.execute("SELECT COUNT(*) FROM \"Transaction\" WHERE type='purchase'").fetchone()[0]
    sale_count=db.execute("SELECT COUNT(*) FROM \"Transaction\" WHERE type='sale'").fetchone()[0]
    type_counts=[purchase_count, sale_count]

    return render_template('reports.html',
                           total_value=total_value,
                           low_stock_count=low_stock_count,
                           supplier_count=supplier_count,
                           low_stock_products=low_stock_products_raw,
                           sales_months=sales_months,
                           sales_values=sales_values,
                           net_sales_months=net_sales_months,
                           net_sales_values=net_sales_values,
                           net_profit_months=net_profit_months,
                           net_profit_values=net_profit_values,
                           total_net_profit=total_net_profit,
                           top_products_names=top_products_names,
                           top_products_counts=top_products_counts,
                           type_counts=type_counts)

@app.route('/api/suggest_reorder/<int:product_id>', methods=['GET'])
def api_suggest_reorder(product_id):
    response_data,status_code=suggest_reorder_quantity_from_ai(product_id)
    return jsonify(response_data),status_code


# --- Settings Routes ---

def get_settings():
    return dict(get_db().execute('SELECT * FROM ShopSettings WHERE id=1').fetchone())

def get_or_create_party(db, name, party_type):
    """Find party by name+type or create it, return party_id."""
    row = db.execute('SELECT id FROM Party WHERE name=? AND type=?', (name, party_type)).fetchone()
    if row:
        return row['id']
    cur = db.cursor()
    cur.execute('INSERT INTO Party (name, type) VALUES (?,?)', (name, party_type))
    return cur.lastrowid

@app.route('/settings', methods=['GET', 'POST'])
def settings():
    db = get_db()
    if request.method == 'POST':
        f = request.form
        db.execute('''
            UPDATE ShopSettings SET
                shop_name=?, owner_name=?, address=?, phone=?, email=?, gstin=?,
                default_cgst=?, default_sgst=?, terms=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=1
        ''', (
            f.get('shop_name',''), f.get('owner_name',''), f.get('address',''),
            f.get('phone',''), f.get('email',''), f.get('gstin',''),
            float(f.get('default_cgst') or 0), float(f.get('default_sgst') or 0),
            f.get('terms','')
        ))
        db.commit()
        flash('Settings saved successfully!', 'success')
        return redirect(url_for('settings'))
    s = get_settings()
    return render_template('settings.html', s=s)


# --- Party Routes ---

@app.route('/parties')
def parties():
    db = get_db()
    all_parties = db.execute('SELECT * FROM Party ORDER BY type, name').fetchall()
    return render_template('parties.html', parties=all_parties)

@app.route('/add_party', methods=['POST'])
def add_party():
    db = get_db()
    f = request.form
    try:
        db.execute('''
            INSERT INTO Party (name, type, phone, email, address, gstin, opening_balance)
            VALUES (?,?,?,?,?,?,?)
        ''', (f['name'], f['type'], f.get('phone',''), f.get('email',''),
              f.get('address',''), f.get('gstin',''),
              float(f.get('opening_balance') or 0)))
        db.commit()
        flash('Party added!', 'success')
    except sqlite3.IntegrityError:
        flash(f'Party "{f["name"]}" already exists.', 'danger')
    return redirect(url_for('parties'))

@app.route('/delete_party/<int:party_id>', methods=['POST'])
def delete_party(party_id):
    db = get_db()
    db.execute('DELETE FROM Party WHERE id=?', (party_id,))
    db.commit()
    flash('Party deleted.', 'success')
    return redirect(url_for('parties'))

@app.route('/party_ledger/<int:party_id>')
def party_ledger(party_id):
    db = get_db()
    party = db.execute('SELECT * FROM Party WHERE id=?', (party_id,)).fetchone()
    if not party:
        flash('Party not found.', 'danger')
        return redirect(url_for('parties'))
    # Only fetch entries where the account_title matches the party name
    # This excludes contra entries (Purchases Account, Sales Account etc.) that were wrongly linked
    entries = db.execute('''
        SELECT * FROM LedgerEntry
        WHERE party_id=? AND account_title=?
        ORDER BY entry_date ASC, id ASC
    ''', (party_id, party['name'])).fetchall()
    total_debit  = sum(e['debit']  for e in entries)
    total_credit = sum(e['credit'] for e in entries)
    balance = total_credit - total_debit
    return render_template('party_ledger.html', party=dict(party),
                           entries=entries,
                           total_debit=round(total_debit,2),
                           total_credit=round(total_credit,2),
                           balance=round(balance,2))


# --- Billing Routes ---

@app.route('/billing')
def billing():
    db = get_db()
    invoices = db.execute('SELECT * FROM Invoice ORDER BY created_at DESC').fetchall()
    products = db.execute('''
        SELECT p.*, s.name AS supplier_name
        FROM Product p LEFT JOIN Supplier s ON p.supplier_id=s.id
        ORDER BY p.name
    ''').fetchall()
    now  = datetime.now().strftime('%Y-%m-%d')
    shop = get_settings()
    return render_template('billing.html', invoices=invoices, products=products, now=now, shop=shop)


@app.route('/create_invoice', methods=['POST'])
def create_invoice():
    db = get_db()
    f  = request.form
    inv_num = f.get('invoice_number','').strip()
    if not inv_num:
        last = db.execute("SELECT invoice_number FROM Invoice ORDER BY id DESC LIMIT 1").fetchone()
        try:
            num = int((last['invoice_number'] if last else 'INV0').replace('INV','')) + 1
        except Exception:
            num = 1
        inv_num = f'INV{num}'

    # collect line items
    descriptions = f.getlist('item_description[]')
    quantities   = f.getlist('quantity[]')
    unit_prices  = f.getlist('unit_price[]')             # unit price GST-inclusive
    gst_rates    = f.getlist('item_gst_rate[]')
    batches      = f.getlist('batch[]')
    mfg_dates    = f.getlist('mfg_date[]')
    exp_dates    = f.getlist('exp_date[]')
    manufacturers= f.getlist('manufacturer[]')
    product_ids  = f.getlist('item_product_id[]')

    total_taxable = 0.0
    total_cgst    = 0.0
    total_sgst    = 0.0
    items_data    = []

    for i, desc in enumerate(descriptions):
        if not desc.strip():
            continue
        unit_p    = float(unit_prices[i])  if unit_prices[i]  else 0.0
        gst_pct   = float(gst_rates[i])   if gst_rates[i]    else 0.0
        qty       = int(quantities[i])     if quantities[i]   else 1
        incl_amt  = round(unit_p * qty, 2)
        taxable   = round(incl_amt / (1 + gst_pct / 100), 2) if gst_pct else incl_amt
        tax_amt   = round(incl_amt - taxable, 2)
        cgst_amt  = round(tax_amt / 2, 2)
        sgst_amt  = round(tax_amt - cgst_amt, 2)
        rate_per_unit = unit_p
        total_taxable += taxable
        total_cgst    += cgst_amt
        total_sgst    += sgst_amt
        # Lookup last purchase price for this product as cost price
        cost_price = 0.0
        pid = int(product_ids[i]) if product_ids[i] else None
        if pid:
            cp_row = db.execute('''
                SELECT unit_price FROM "Transaction"
                WHERE product_id=? AND type='purchase'
                ORDER BY id DESC LIMIT 1
            ''', (pid,)).fetchone()
            if cp_row:
                cost_price = cp_row['unit_price']
        items_data.append({
            'product_id':   pid,
            'description':  desc.strip(),
            'batch':        batches[i]       if i < len(batches)       else '',
            'mfg_date':     mfg_dates[i]     if i < len(mfg_dates)     else '',
            'exp_date':     exp_dates[i]     if i < len(exp_dates)     else '',
            'manufacturer': manufacturers[i] if i < len(manufacturers) else '',
            'quantity':     qty,
            'rate':         rate_per_unit,
            'tax_label':    f'GST {gst_pct}%',
            'tax_rate':     gst_pct,
            'incl_amount':  incl_amt,
            'cgst':         cgst_amt,
            'sgst':         sgst_amt,
            'cost_price':   cost_price,
        })

    round_off   = float(f.get('round_off') or 0)
    grand_total = round(total_taxable + total_cgst + total_sgst + round_off, 2)
    total_cost  = round(sum(it['cost_price'] * it['quantity'] for it in items_data), 2)
    net_profit  = round(grand_total - total_cost, 2)
    bill_type   = f.get('bill_type', 'cash')
    shop        = get_settings()

    try:
        cursor = db.cursor()
        cursor.execute('''
            INSERT INTO Invoice (
                invoice_number, invoice_date, due_date,
                bill_to_name, bill_to_gstin,
                ship_to_mobile, terms,
                cgst_rate, sgst_rate, cgst2_rate, sgst2_rate, round_off, status,
                bill_type, total_taxable, total_cgst, total_sgst, grand_total, net_profit
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ''', (
            inv_num, f['invoice_date'], f.get('due_date', f['invoice_date']),
            f.get('bill_to_name',''), f.get('bill_to_gstin',''),
            f.get('ship_to_mobile',''), f.get('terms', shop['terms']),
            shop['default_cgst'], shop['default_sgst'], 0, 0,
            round_off, 'paid',
            bill_type,
            round(total_taxable,2), round(total_cgst,2), round(total_sgst,2), grand_total, net_profit
        ))
        invoice_id = cursor.lastrowid

        for item in items_data:
            cursor.execute('''
                INSERT INTO InvoiceItem
                    (invoice_id, product_id, item_description, batch, mfg_date, exp_date,
                     manufacturer, quantity, rate, tax_label, tax_rate, cost_price)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ''', (
                invoice_id, item['product_id'], item['description'],
                item['batch'], item['mfg_date'], item['exp_date'], item['manufacturer'],
                item['quantity'], item['rate'], item['tax_label'], item['tax_rate'], item['cost_price']
            ))
            # Deduct stock and record sale transaction
            if item['product_id']:
                cursor.execute('''
                    UPDATE Product SET stock_quantity = stock_quantity - ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                ''', (item['quantity'], item['product_id']))
                cursor.execute('''
                    INSERT INTO "Transaction" (product_id, quantity, type, date, unit_price)
                    VALUES (?, ?, 'sale', ?, ?)
                ''', (item['product_id'], item['quantity'], f['invoice_date'], item['rate']))

        # --- Auto ledger posting ---
        last_group = db.execute('SELECT MAX(group_id) FROM LedgerEntry').fetchone()[0]
        group_id   = (last_group or 0) + 1
        inv_date   = f['invoice_date']
        bill_type  = f.get('bill_type', 'cash')
        customer   = f.get('bill_to_name', 'Customer')

        if bill_type == 'cash':
            cursor.execute('''
                INSERT INTO LedgerEntry (entry_date,account_title,explanation,ref,debit,credit,group_id,invoice_id)
                VALUES (?,?,?,?,?,?,?,?)
            ''', (inv_date,'Cash Account',f'Cash sale - {inv_num}',inv_num,grand_total,0,group_id,invoice_id))
            cursor.execute('''
                INSERT INTO LedgerEntry (entry_date,account_title,explanation,ref,debit,credit,group_id,invoice_id)
                VALUES (?,?,?,?,?,?,?,?)
            ''', (inv_date,'Sales Account',f'Cash sale - {inv_num}',inv_num,0,round(total_taxable,2),group_id,invoice_id))
        else:
            cust_party_id = get_or_create_party(db, customer, 'customer')
            # Dr Customer (party link) / Cr Sales (no party link)
            cursor.execute('''
                INSERT INTO LedgerEntry (entry_date,account_title,explanation,ref,debit,credit,group_id,invoice_id,party_id)
                VALUES (?,?,?,?,?,?,?,?,?)
            ''', (inv_date,customer,f'Credit sale - {inv_num}',inv_num,grand_total,0,group_id,invoice_id,cust_party_id))
            cursor.execute('''
                INSERT INTO LedgerEntry (entry_date,account_title,explanation,ref,debit,credit,group_id,invoice_id)
                VALUES (?,?,?,?,?,?,?,?)
            ''', (inv_date,'Sales Account',f'Credit sale - {inv_num}',inv_num,0,round(total_taxable,2),group_id,invoice_id))

        if total_cgst > 0:
            cursor.execute('''
                INSERT INTO LedgerEntry (entry_date,account_title,explanation,ref,debit,credit,group_id,invoice_id)
                VALUES (?,?,?,?,?,?,?,?)
            ''', (inv_date,'CGST Payable',f'CGST on {inv_num}',inv_num,0,round(total_cgst,2),group_id,invoice_id))
        if total_sgst > 0:
            cursor.execute('''
                INSERT INTO LedgerEntry (entry_date,account_title,explanation,ref,debit,credit,group_id,invoice_id)
                VALUES (?,?,?,?,?,?,?,?)
            ''', (inv_date,'SGST Payable',f'SGST on {inv_num}',inv_num,0,round(total_sgst,2),group_id,invoice_id))

        db.commit()
        flash(f'Invoice {inv_num} created!', 'success')
        return redirect(url_for('invoice_detail', invoice_id=invoice_id))
    except Exception as e:
        db.rollback()
        flash(f'Error creating invoice: {e}', 'danger')
        return redirect(url_for('billing'))


@app.route('/invoice/<int:invoice_id>')
def invoice_detail(invoice_id):
    db = get_db()
    invoice = db.execute('''
        SELECT i.*, s.name AS supplier_name, s.contact_email AS supplier_email,
               p.name AS party_name, p.phone AS party_phone
        FROM Invoice i
        LEFT JOIN Supplier s ON i.supplier_id = s.id
        LEFT JOIN Party p ON i.party_id = p.id
        WHERE i.id = ?
    ''', (invoice_id,)).fetchone()
    if not invoice:
        flash('Invoice not found.', 'danger')
        return redirect(url_for('billing'))
    items = db.execute('SELECT * FROM InvoiceItem WHERE invoice_id = ? ORDER BY id', (invoice_id,)).fetchall()
    shop  = get_settings()
    return render_template('invoice_detail.html', invoice=dict(invoice), items=items, shop=shop)


@app.route('/invoice/<int:invoice_id>/print')
def invoice_print(invoice_id):
    db = get_db()
    invoice = db.execute('''
        SELECT i.*, s.name AS supplier_name, s.contact_email AS supplier_email,
               p.name AS party_name, p.phone AS party_phone
        FROM Invoice i
        LEFT JOIN Supplier s ON i.supplier_id = s.id
        LEFT JOIN Party p ON i.party_id = p.id
        WHERE i.id = ?
    ''', (invoice_id,)).fetchone()
    if not invoice:
        flash('Invoice not found.', 'danger')
        return redirect(url_for('billing'))
    items = db.execute('SELECT * FROM InvoiceItem WHERE invoice_id = ? ORDER BY id', (invoice_id,)).fetchall()
    shop  = get_settings()
    return render_template('invoice_print.html', invoice=dict(invoice), items=items, shop=shop)


@app.route('/delete_invoice/<int:invoice_id>', methods=['POST'])
def delete_invoice(invoice_id):
    db = get_db()
    try:
        db.execute('DELETE FROM Invoice WHERE id = ?', (invoice_id,))
        db.commit()
        flash('Invoice deleted.', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('billing'))


# --- Ledger Routes ---

@app.route('/ledger')
def ledger():
    db = get_db()
    entries = db.execute('''
        SELECT * FROM LedgerEntry ORDER BY entry_date ASC, group_id ASC, id ASC
    ''').fetchall()
    total_debit  = db.execute('SELECT COALESCE(SUM(debit),0)  FROM LedgerEntry').fetchone()[0]
    total_credit = db.execute('SELECT COALESCE(SUM(credit),0) FROM LedgerEntry').fetchone()[0]
    return render_template('ledger.html', entries=entries,
                           total_debit=round(total_debit, 2),
                           total_credit=round(total_credit, 2))


@app.route('/add_ledger_entry', methods=['POST'])
def add_ledger_entry():
    db = get_db()
    f = request.form
    dates         = f.getlist('entry_date[]')
    accounts      = f.getlist('account_title[]')
    explanations  = f.getlist('explanation[]')
    refs          = f.getlist('ref[]')
    debits        = f.getlist('debit[]')
    credits       = f.getlist('credit[]')

    if not accounts:
        flash('No entries provided.', 'danger')
        return redirect(url_for('ledger'))

    # all rows in one form submission share the same group_id
    last_group = db.execute('SELECT MAX(group_id) FROM LedgerEntry').fetchone()[0]
    group_id = (last_group or 0) + 1
    entry_date = dates[0] if dates else datetime.now().strftime('%Y-%m-%d')

    try:
        cursor = db.cursor()
        for i, account in enumerate(accounts):
            if not account.strip():
                continue
            cursor.execute('''
                INSERT INTO LedgerEntry (entry_date, account_title, explanation, ref, debit, credit, group_id)
                VALUES (?,?,?,?,?,?,?)
            ''', (
                dates[i] if i < len(dates) and dates[i] else entry_date,
                account.strip(),
                explanations[i] if i < len(explanations) else '',
                refs[i] if i < len(refs) else '',
                float(debits[i]) if i < len(debits) and debits[i] else 0,
                float(credits[i]) if i < len(credits) and credits[i] else 0,
                group_id
            ))
        db.commit()
        flash('Journal entry added successfully!', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Error adding entry: {e}', 'danger')
    return redirect(url_for('ledger'))


@app.route('/delete_ledger_group/<int:group_id>', methods=['POST'])
def delete_ledger_group(group_id):
    db = get_db()
    try:
        db.execute('DELETE FROM LedgerEntry WHERE group_id = ?', (group_id,))
        db.commit()
        flash('Journal entry deleted.', 'success')
    except Exception as e:
        flash(f'Error: {e}', 'danger')
    return redirect(url_for('ledger'))


@app.route('/delete_all_data', methods=['POST'])
def delete_all_data():
    db = get_db()
    try:
        db.execute('DELETE FROM LedgerEntry')
        db.execute('DELETE FROM InvoiceItem')
        db.execute('DELETE FROM Invoice')
        db.execute('DELETE FROM "Transaction"')
        db.execute('DELETE FROM Product')
        db.execute('DELETE FROM Party')
        db.execute('DELETE FROM Supplier')
        db.commit()
        flash('All data deleted. Shop settings kept.', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Error deleting data: {e}', 'danger')
    return redirect(url_for('settings'))


# --- Stock Routes ---

@app.route('/stock')
def stock():
    db = get_db()
    search = request.args.get('q', '').strip()
    if search:
        items = db.execute('''
            SELECT p.*, s.name AS supplier_name
            FROM Product p LEFT JOIN Supplier s ON p.supplier_id=s.id
            WHERE p.name LIKE ? OR p.sku LIKE ?
            ORDER BY p.name
        ''', (f'%{search}%', f'%{search}%')).fetchall()
    else:
        items = db.execute('''
            SELECT p.*, s.name AS supplier_name
            FROM Product p LEFT JOIN Supplier s ON p.supplier_id=s.id
            ORDER BY p.name
        ''').fetchall()
    total_value = db.execute('SELECT COALESCE(SUM(stock_quantity*unit_price),0) FROM Product').fetchone()[0]
    return render_template('stock.html', items=items, search=search, total_value=round(total_value,2))


@app.route('/add_stock', methods=['GET', 'POST'])
def add_stock():
    db = get_db()
    suppliers = get_all_suppliers()
    if request.method == 'POST':
        f = request.form
        supplier_name = f.get('supplier_name', '').strip()
        bill_date     = f.get('bill_date', datetime.now().strftime('%Y-%m-%d'))
        bill_ref      = f.get('bill_ref', '').strip()
        dm_number     = f.get('dm_number', '').strip()
        bill_type     = f.get('bill_type', 'cash')

        names     = f.getlist('item_name[]')
        companies = f.getlist('company[]')
        skus      = f.getlist('sku[]')
        qtys      = f.getlist('quantity[]')
        pur_prices= f.getlist('purchase_price[]')  # GST inclusive
        gst_rates = f.getlist('gst_rate[]')
        sell_prices = f.getlist('selling_price[]') # GST inclusive selling price

        # get or create supplier
        sup_row = db.execute('SELECT id FROM Supplier WHERE name=?', (supplier_name,)).fetchone()
        if sup_row:
            supplier_id = sup_row['id']
        else:
            cur = db.cursor()
            cur.execute('INSERT INTO Supplier (name) VALUES (?)', (supplier_name,))
            supplier_id = cur.lastrowid

        sup_party_id = get_or_create_party(db, supplier_name, 'supplier')

        last_group = db.execute('SELECT MAX(group_id) FROM LedgerEntry').fetchone()[0]
        grp = (last_group or 0) + 1
        total_bill = 0.0
        cursor = db.cursor()

        try:
            for i, name in enumerate(names):
                if not name.strip():
                    continue
                qty       = int(qtys[i])          if qtys[i]       else 1
                pur_price = float(pur_prices[i])  if pur_prices[i] else 0.0
                sell_price= float(sell_prices[i]) if sell_prices[i] else pur_price
                sku       = skus[i].strip()        if skus[i].strip() else f'SKU-{name[:4].upper()}-{i+1}'
                company   = companies[i].strip()   if i < len(companies) else ''
                gst_pct   = float(gst_rates[i])   if gst_rates[i]  else 0.0
                item_total = round(pur_price * qty, 2)
                total_bill += item_total

                # upsert product: if SKU exists update stock, else insert
                existing = db.execute('SELECT id, stock_quantity FROM Product WHERE sku=?', (sku,)).fetchone()
                if existing:
                    new_qty = existing['stock_quantity'] + qty
                    cursor.execute('''
                        UPDATE Product SET stock_quantity=?, unit_price=?, supplier_id=?, company=?, updated_at=CURRENT_TIMESTAMP
                        WHERE id=?
                    ''', (new_qty, sell_price, supplier_id, company, existing['id']))
                    prod_id = existing['id']
                else:
                    cursor.execute('''
                        INSERT INTO Product (name, sku, stock_quantity, unit_price, supplier_id, company)
                        VALUES (?,?,?,?,?,?)
                    ''', (name.strip(), sku, qty, sell_price, supplier_id, company))
                    prod_id = cursor.lastrowid

                # record purchase transaction
                cursor.execute('''
                    INSERT INTO "Transaction" (product_id, quantity, type, date, unit_price)
                    VALUES (?,?,?,?,?)
                ''', (prod_id, qty, 'purchase', bill_date, pur_price))

            ref_label = bill_ref or dm_number or f'PUR-{bill_date}'
            if bill_type == 'cash':
                # Dr Purchases (no party) / Cr Cash (no party)
                cursor.execute('''
                    INSERT INTO LedgerEntry (entry_date,account_title,explanation,ref,debit,credit,group_id)
                    VALUES (?,?,?,?,?,?,?)
                ''', (bill_date,'Purchases Account',f'Cash purchase - {supplier_name}',ref_label,round(total_bill,2),0,grp))
                cursor.execute('''
                    INSERT INTO LedgerEntry (entry_date,account_title,explanation,ref,debit,credit,group_id)
                    VALUES (?,?,?,?,?,?,?)
                ''', (bill_date,'Cash Account',f'Cash purchase - {supplier_name}',ref_label,0,round(total_bill,2),grp))
            else:
                # Dr Purchases (no party link) / Cr Supplier (party link — they appear in supplier ledger)
                cursor.execute('''
                    INSERT INTO LedgerEntry (entry_date,account_title,explanation,ref,debit,credit,group_id)
                    VALUES (?,?,?,?,?,?,?)
                ''', (bill_date,'Purchases Account',f'Credit purchase - {supplier_name}',ref_label,round(total_bill,2),0,grp))
                cursor.execute('''
                    INSERT INTO LedgerEntry (entry_date,account_title,explanation,ref,debit,credit,group_id,party_id)
                    VALUES (?,?,?,?,?,?,?,?)
                ''', (bill_date,supplier_name,f'Credit purchase - {supplier_name}',ref_label,0,round(total_bill,2),grp,sup_party_id))

            db.commit()
            flash(f'Stock added successfully! Total bill: ₹{round(total_bill,2)}', 'success')
            return redirect(url_for('stock'))
        except Exception as e:
            db.rollback()
            flash(f'Error adding stock: {e}', 'danger')

    return render_template('add_stock.html', suppliers=suppliers, now=datetime.now().strftime('%Y-%m-%d'))


# --- Cash Receipt Routes ---

@app.route('/cash_receipt')
def cash_receipt():
    db = get_db()
    # customers with their outstanding balance (credit > debit = they owe us)
    customers = db.execute('''
        SELECT p.id, p.name,
               COALESCE(SUM(CASE WHEN le.account_title=p.name THEN le.debit ELSE 0 END),0)
               - COALESCE(SUM(CASE WHEN le.account_title=p.name THEN le.credit ELSE 0 END),0)
               + COALESCE(p.opening_balance,0) AS balance
        FROM Party p
        LEFT JOIN LedgerEntry le ON le.party_id = p.id
        WHERE p.type = 'customer'
        GROUP BY p.id
        ORDER BY p.name
    ''').fetchall()

    receipts = db.execute('''
        SELECT cr.receipt_ref, cr.payment_date, cr.party_id, p.name AS party_name,
               SUM(cr.amount) AS total_amount
        FROM CashReceipt cr
        JOIN Party p ON cr.party_id = p.id
        GROUP BY cr.receipt_ref
        ORDER BY cr.created_at DESC
        LIMIT 50
    ''').fetchall()
    now = datetime.now().strftime('%Y-%m-%d')
    return render_template('cash_receipt.html', customers=customers, receipts=receipts, now=now)


@app.route('/post_cash_receipt', methods=['POST'])
def post_cash_receipt():
    db = get_db()
    f = request.form
    party_id     = int(f['party_id'])
    amount       = round(float(f['amount']), 2)
    payment_date = f['payment_date']
    receipt_ref  = f.get('receipt_ref', '').strip()

    if amount <= 0:
        flash('Amount must be positive.', 'danger')
        return redirect(url_for('cash_receipt'))

    # auto-generate ref if blank
    if not receipt_ref:
        last = db.execute("SELECT receipt_ref FROM CashReceipt ORDER BY id DESC LIMIT 1").fetchone()
        try:
            num = int((last['receipt_ref'] if last else 'RCP0').replace('RCP','')) + 1
        except Exception:
            num = 1
        receipt_ref = f'RCP{num:04d}'

    MAX_PER_RECEIPT = 10000.0
    remaining = amount
    splits = []
    while remaining > 0:
        chunk = min(remaining, MAX_PER_RECEIPT)
        splits.append(round(chunk, 2))
        remaining = round(remaining - chunk, 2)

    party = db.execute('SELECT name FROM Party WHERE id=?', (party_id,)).fetchone()
    party_name = party['name'] if party else 'Customer'

    last_group = db.execute('SELECT MAX(group_id) FROM LedgerEntry').fetchone()[0]
    grp = (last_group or 0) + 1

    try:
        cursor = db.cursor()
        for chunk in splits:
            # Dr Cash / Cr Customer (reduces what they owe)
            cursor.execute('''
                INSERT INTO LedgerEntry (entry_date,account_title,explanation,ref,debit,credit,group_id,party_id)
                VALUES (?,?,?,?,?,?,?,?)
            ''', (payment_date,'Cash Account',f'Payment received - {party_name}',receipt_ref,chunk,0,grp,party_id))
            cursor.execute('''
                INSERT INTO LedgerEntry (entry_date,account_title,explanation,ref,debit,credit,group_id,party_id)
                VALUES (?,?,?,?,?,?,?,?)
            ''', (payment_date,party_name,f'Payment received - {party_name}',receipt_ref,0,chunk,grp,party_id))
            cursor.execute('''
                INSERT INTO CashReceipt (party_id,payment_date,amount,receipt_ref,ledger_group_id)
                VALUES (?,?,?,?,?)
            ''', (party_id, payment_date, chunk, receipt_ref, grp))

        db.commit()
        flash(f'Payment of ₹{amount:,.2f} posted. {len(splits)} receipt(s) generated.', 'success')
        return redirect(url_for('print_cash_receipt', receipt_group=receipt_ref))
    except Exception as e:
        db.rollback()
        flash(f'Error posting payment: {e}', 'danger')
        return redirect(url_for('cash_receipt'))


@app.route('/cash_receipt/print/<receipt_group>')
def print_cash_receipt(receipt_group):
    db = get_db()
    rows = db.execute('''
        SELECT cr.*, p.name AS party_name
        FROM CashReceipt cr
        JOIN Party p ON cr.party_id = p.id
        WHERE cr.receipt_ref = ?
        ORDER BY cr.id
    ''', (receipt_group,)).fetchall()
    if not rows:
        flash('Receipt not found.', 'danger')
        return redirect(url_for('cash_receipt'))
    shop = get_settings()
    return render_template('cash_receipt_print.html', receipts=rows, shop=shop)


# --- Supplier Payment Routes ---

@app.route('/supplier_payment')
def supplier_payment():
    db = get_db()
    suppliers = db.execute('''
        SELECT p.id, p.name,
               COALESCE(SUM(CASE WHEN le.account_title=p.name THEN le.credit ELSE 0 END),0)
               - COALESCE(SUM(CASE WHEN le.account_title=p.name THEN le.debit ELSE 0 END),0)
               + COALESCE(p.opening_balance,0) AS balance
        FROM Party p
        LEFT JOIN LedgerEntry le ON le.party_id = p.id
        WHERE p.type = 'supplier'
        GROUP BY p.id
        ORDER BY p.name
    ''').fetchall()
    payments = db.execute('''
        SELECT sp.*, p.name AS party_name
        FROM SupplierPayment sp
        JOIN Party p ON sp.party_id = p.id
        ORDER BY sp.created_at DESC
        LIMIT 50
    ''').fetchall()
    now = datetime.now().strftime('%Y-%m-%d')
    return render_template('supplier_payment.html', suppliers=suppliers, payments=payments, now=now)


@app.route('/post_supplier_payment', methods=['POST'])
def post_supplier_payment():
    db = get_db()
    f            = request.form
    party_id     = int(f['party_id'])
    amount       = round(float(f['amount']), 2)
    payment_date = f['payment_date']
    receipt_ref  = f.get('receipt_ref', '').strip()
    payment_mode = f.get('payment_mode', 'Cash')
    notes        = f.get('notes', '').strip()

    if amount <= 0:
        flash('Amount must be positive.', 'danger')
        return redirect(url_for('supplier_payment'))

    party = db.execute('SELECT name FROM Party WHERE id=?', (party_id,)).fetchone()
    if not party:
        flash('Supplier not found.', 'danger')
        return redirect(url_for('supplier_payment'))
    party_name = party['name']

    last_group = db.execute('SELECT MAX(group_id) FROM LedgerEntry').fetchone()[0]
    grp = (last_group or 0) + 1
    explanation = notes or f'Payment to {party_name} via {payment_mode}'

    try:
        cursor = db.cursor()
        # Dr Supplier (reduces what we owe) / Cr Cash (money goes out)
        cursor.execute('''
            INSERT INTO LedgerEntry (entry_date,account_title,explanation,ref,debit,credit,group_id,party_id)
            VALUES (?,?,?,?,?,?,?,?)
        ''', (payment_date, party_name, explanation, receipt_ref, amount, 0, grp, party_id))
        cursor.execute('''
            INSERT INTO LedgerEntry (entry_date,account_title,explanation,ref,debit,credit,group_id)
            VALUES (?,?,?,?,?,?,?)
        ''', (payment_date, f'{payment_mode} Account', explanation, receipt_ref, 0, amount, grp))
        cursor.execute('''
            INSERT INTO SupplierPayment (party_id,payment_date,amount,receipt_ref,payment_mode,notes,ledger_group_id)
            VALUES (?,?,?,?,?,?,?)
        ''', (party_id, payment_date, amount, receipt_ref, payment_mode, notes, grp))
        db.commit()
        flash(f'Payment of ₹{amount:,.2f} to {party_name} recorded. Receipt: {receipt_ref}', 'success')
    except Exception as e:
        db.rollback()
        flash(f'Error recording payment: {e}', 'danger')
    return redirect(url_for('supplier_payment'))


# --- All Invoices & DM Memo Routes ---

@app.route('/all_invoices')
def all_invoices():
    db = get_db()
    invoices = db.execute('SELECT * FROM Invoice ORDER BY invoice_date DESC, id DESC').fetchall()
    total_revenue = db.execute('SELECT COALESCE(SUM(grand_total),0) FROM Invoice').fetchone()[0]
    return render_template('all_invoices.html', invoices=invoices, total_revenue=round(total_revenue,2))


@app.route('/dm_memo')
def dm_memo():
    db = get_db()
    memos = db.execute('SELECT * FROM DMMemo ORDER BY created_at DESC').fetchall()
    products = db.execute('SELECT p.*, s.name AS supplier_name FROM Product p LEFT JOIN Supplier s ON p.supplier_id=s.id ORDER BY p.name').fetchall()
    now = datetime.now().strftime('%Y-%m-%d')
    return render_template('dm_memo.html', memos=memos, products=products, now=now)


@app.route('/create_dm_memo', methods=['POST'])
def create_dm_memo():
    db = get_db()
    f = request.form
    dm_num = f.get('dm_number','').strip()
    if not dm_num:
        last = db.execute('SELECT dm_number FROM DMMemo ORDER BY id DESC LIMIT 1').fetchone()
        try:
            num = int((last['dm_number'] if last else 'DM0').replace('DM','')) + 1
        except Exception:
            num = 1
        dm_num = f'DM{num:04d}'
    try:
        cursor = db.cursor()
        cursor.execute('''
            INSERT INTO DMMemo (dm_number,dm_date,party_name,party_mobile,party_address,transport,ref_invoice,remarks)
            VALUES (?,?,?,?,?,?,?,?)
        ''', (dm_num, f['dm_date'], f['party_name'], f.get('party_mobile',''),
              f.get('party_address',''), f.get('transport',''), f.get('ref_invoice',''), f.get('remarks','')))
        dm_id = cursor.lastrowid
        names = f.getlist('item_name[]')
        for i, name in enumerate(names):
            if not name.strip(): continue
            cursor.execute('''
                INSERT INTO DMMemoItem (dm_id,item_name,company,batch,quantity,unit)
                VALUES (?,?,?,?,?,?)
            ''', (dm_id, name.strip(),
                  f.getlist('company[]')[i] if i < len(f.getlist('company[]')) else '',
                  f.getlist('batch[]')[i]   if i < len(f.getlist('batch[]'))   else '',
                  int(f.getlist('quantity[]')[i]) if i < len(f.getlist('quantity[]')) else 1,
                  f.getlist('unit[]')[i]    if i < len(f.getlist('unit[]'))    else 'Nos'))
        db.commit()
        flash(f'DM Memo {dm_num} created!', 'success')
        return redirect(url_for('print_dm_memo', dm_id=dm_id))
    except Exception as e:
        db.rollback()
        flash(f'Error creating DM Memo: {e}', 'danger')
        return redirect(url_for('dm_memo'))


@app.route('/dm_memo/print/<int:dm_id>')
def print_dm_memo(dm_id):
    db = get_db()
    memo = db.execute('SELECT * FROM DMMemo WHERE id=?', (dm_id,)).fetchone()
    if not memo:
        flash('DM Memo not found.', 'danger')
        return redirect(url_for('dm_memo'))
    items = db.execute('SELECT * FROM DMMemoItem WHERE dm_id=? ORDER BY id', (dm_id,)).fetchall()
    shop = get_settings()
    return render_template('dm_memo_print.html', memo=dict(memo), items=items, shop=shop)


@app.route('/delete_dm_memo/<int:dm_id>', methods=['POST'])
def delete_dm_memo(dm_id):
    db = get_db()
    db.execute('DELETE FROM DMMemo WHERE id=?', (dm_id,))
    db.commit()
    flash('DM Memo deleted.', 'success')
    return redirect(url_for('dm_memo'))


# --- AI Service Routes ---

@app.route('/ai_service', methods=['GET', 'POST'])
def ai_service():
    db = get_db()
    answer = None
    question = ''
    error = None
    # Re-index live data on every page load so context is fresh
    try:
        index_live_data(db)
    except Exception as e:
        error = f'Index error: {e}'

    if request.method == 'POST':
        question = request.form.get('question', '').strip()
        if question:
            try:
                answer = rag_answer(question, db)
            except Exception as e:
                error = f'AI error: {e}'

    # Chat history stored in session-like list via hidden field
    return render_template('ai_service.html', answer=answer, question=question, error=error)


@app.route('/ai_reindex', methods=['POST'])
def ai_reindex():
    try:
        index_live_data(get_db())
        flash('AI knowledge base re-indexed with latest data.', 'success')
    except Exception as e:
        flash(f'Re-index failed: {e}', 'danger')
    return redirect(url_for('ai_service'))


if __name__=='__main__':
    with app.app_context():
        init_db()
        seed_static_knowledge()
    app.run(debug=os.environ.get('FLASK_DEBUG', 'false').lower() == 'true')

