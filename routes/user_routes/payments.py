from flask import request, jsonify
from . import user_routes
import pymysql
import pymysql.cursors
from datetime import datetime, timezone
from database import connect_to_db
from helpers import calculate_fees, format_phone_number
from logger import log_event
import os
import time
import requests
from config import Config

# ====== Payment Fee Info ======
@user_routes.route('/due_payment', methods=['POST'])
def payment():
    conn = connect_to_db()

    data = request.get_json()
    phone = data.get('phone') or ""
    fullname = (data.get('fullname') or 'guest').strip()

    phone = format_phone_number(phone)

    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            cursor.execute("""
                SELECT people.class, people.gender, payment.special_food, payment.reduce_fee,
                    payment.food, payment.due_months AS month, users.phone, users.fullname
                FROM users
                JOIN people ON people.id = users.id
                JOIN payment ON payment.id = users.id
                WHERE users.phone = %s AND LOWER(users.fullname) = LOWER(%s)
            """, (phone, fullname))
            result = cursor.fetchone()

        if not result:
            log_event("payment_user_not_found", phone, f"User {fullname} not found")
            return jsonify({"message": "User not found"}), 404
    except Exception as e:
        conn.rollback()
        log_event("get_payment_failed", phone, f"DB Error: {str(e)}")
        return jsonify({"error": "Transaction failed"}), 500
    finally:
        conn.close()


    # Extract data
    class_name = result['class']
    gender = result['gender']
    special_food = result['special_food']
    reduce_fee = result['reduce_fee']
    food = result['food']
    due_months = result['month']

    # Calculate fees
    fees = calculate_fees(class_name, gender, special_food, reduce_fee, food)

    return jsonify({"amount": fees, "month": due_months}), 200


# ====== Get Transaction History ======
@user_routes.route('/get_transactions', methods=['POST'])
def get_transactions():
    data = request.get_json() or {}
    phone            = data.get('phone')
    fullname         = (data.get('fullname') or '').strip()
    transaction_type = data.get('type')
    lastfetched      = data.get('updatedSince')
    

    # Required fields
    if not phone or not fullname or not transaction_type:
        log_event("payment_missing_fields", phone,
                  "Phone, fullname or transaction type missing")
        return jsonify({"error": "Phone, fullname and type are required"}), 400

    # Normalize phone
    phone = format_phone_number(phone)
    if not phone:
        log_event("payment_invalid_phone", phone, "Invalid phone format")
        return jsonify({"error": "Invalid phone number"}), 400

    # Build base query and params
    sql = """
      SELECT
        t.type,
        t.month     AS details,
        t.amount,
        t.date
      FROM transactions t
      JOIN users        u ON t.id = u.id
      WHERE u.phone = %s
        AND LOWER(u.fullname) = LOWER(%s)
        AND t.type = %s
    """
    params = [phone, fullname, transaction_type]

    # If updatedSince provided, parse & add to WHERE
    if lastfetched:
        try:
            cutoff = datetime.fromisoformat(
                lastfetched.replace("Z", "+00:00")
            )
            sql += " AND t.updated_at > %s"
            params.append(cutoff)
        except ValueError:
            log_event("payment_invalid_timestamp", phone, lastfetched)
            return jsonify({"error": "Invalid updatedSince format"}), 400

    # Final ordering
    sql += " ORDER BY t.date DESC"

    # Execute
    conn = connect_to_db()
    try:
        with conn.cursor(cursor=pymysql.cursors.DictCursor) as cursor:
            cursor.execute(sql, params)
            transactions = cursor.fetchall()
    except Exception as e:
        conn.rollback()
        log_event("payment_transaction_error", phone, str(e))
        return jsonify({"error": "Internal server error"}), 500
    finally:
        conn.close()

    # Handle no‐results
    if not transactions:
        return jsonify({"message": "No transactions found"}), 404

    # Normalize dates to ISO-8601 Z format
    for tx in transactions:
        d = tx.get("date")
        if isinstance(d, datetime):
            tx["date"] = d.astimezone(timezone.utc) \
                         .isoformat().replace("+00:00", "Z")

    # Return payload
    return jsonify({
        "transactions": transactions,
        "lastSyncedAt": datetime.now(timezone.utc)
                              .isoformat().replace("+00:00","Z")
    }), 200

@user_routes.route('/pay_sslcommerz', methods=['POST'])
def pay_sslcommerz():
    data = request.get_json() or {}
    phone            = data.get('phone') or "01XXXXXXXXX"
    fullname         = (data.get('fullname') or 'guest').strip()
    amount           = data.get('amount')
    months           = data.get('months')
    transaction_type = data.get('type')
    email            = (data.get('email') or '').strip()

    # fallback dummy email .
    if not email:
       email = "user@no-reply.annurmadrasa.com"

    if not transaction_type or amount is None:
        log_event("payment_missing_fields", phone, "Missing payment info")
        return jsonify({"error": "amount and type required"}), 400

    tran_id    = f"ssl_{int(time.time())}"
    store_id   = os.getenv("SSLCOMMERZ_STORE_ID")
    store_pass = os.getenv("SSLCOMMERZ_STORE_PASS")
    if not store_id or not store_pass:
        log_event("sslcommerz_config_missing", phone, "SSLCommerz credentials not set")
        return jsonify({"error": "Payment gateway misconfigured"}), 500

    payload = {
        # merchant + txn
        "store_id":      store_id,
        "store_passwd":  store_pass,
        "total_amount":  amount,
        "currency":      "BDT",
        "tran_id":       tran_id,
        "success_url":   Config.BASE_URL + 'payment_success_ssl',
        "fail_url":      Config.BASE_URL + 'payment_fail_ssl',
        "cancel_url":    Config.BASE_URL + 'payment_cancel_ssl',   # new
        "ipn_url":       Config.BASE_URL + 'payment_ipn',          # optional

        # product
        "product_name":      transaction_type.capitalize(),
        "product_category":  transaction_type.capitalize(),
        "product_profile":   "non-physical-goods",

        # customer (Mirpur defaults)
        "cus_name":    fullname,
        "cus_email":   email,
        "cus_add1":    "Mirpur 10",
        "cus_add2":    "", 
        "cus_city":    "Dhaka",
        "cus_postcode":"1216",
        "cus_country": "Bangladesh",
        "cus_phone":   phone,

        # passthrough fields
        "value_a": phone,
        "value_b": fullname,
        "value_c": months or '',
        "value_d": transaction_type,
        "value_e": tran_id,

        # others
        "emi_option": 0,
        "shipping_method": "NO",
        "num_of_item": 1,
        "weight_of_items": 0.5,
        "logistic_pickup_id": "madrasaid123",
        "logistic_delivery_type": "madrasadilevery_by_air"
    }

    try:
        log_event("sslcommerz_request", phone, f"Initiating {tran_id} for {amount}")
        r   = requests.post(
            'https://sandbox.sslcommerz.com/gwprocess/v4/api.php',
            data=payload,
            timeout=10
        )
        res = r.json()
    except Exception as e:
        log_event("sslcommerz_request_error", phone, str(e))
        return jsonify({"error": "Gateway unreachable"}), 502

    if res.get('status') == 'SUCCESS':
        return jsonify({"GatewayPageURL": res.get('GatewayPageURL')}), 200
    else:
        log_event(
            "sslcommerz_initiation_failed",
            phone,
            f"{res.get('status')} – {res.get('failedreason')}"
        )
        return jsonify({
            "error":  "Payment initiation failed",
            "reason": res.get('failedreason')
        }), 400


@user_routes.route('/payment_success_ssl', methods=['POST'])
def payment_success_ssl():
    data            = request.form.to_dict() or {}
    phone           = data.get('value_a')
    fullname        = data.get('value_b')
    amount          = data.get('amount')
    months          = data.get('value_c')
    transaction_type= data.get('value_d')
    tran_id         = data.get('value_e')

    # Ensure we received our transaction identifier back
    if not tran_id:
        log_event("sslcommerz_callback_no_tranid", phone, "Missing value_e")
        return jsonify({"error": "Missing transaction identifier"}), 400

    phone = format_phone_number(phone)

    # 1️⃣ Validate callback with SSLCommerz
    store_id   = os.getenv("SSLCOMMERZ_STORE_ID")
    store_pass = os.getenv("SSLCOMMERZ_STORE_PASS")
    try:
        validation = requests.get(
            'https://sandbox.sslcommerz.com/validator/api/validationserverAPI',
            params={
                'tran_id':      tran_id,
                'store_id':     store_id,
                'store_passwd': store_pass
            },
            timeout=10
        ).json()
        if validation.get('status') != 'VALID':
            log_event("sslcommerz_validation_failed", phone, validation.get('status'))
            return jsonify({"error": "Payment validation failed"}), 400
    except Exception as e:
        log_event("sslcommerz_validation_error", phone, str(e))
        return jsonify({"error": "Payment validation error"}), 502

    # 2️⃣ Record transaction directly in the database
    db = None
    try:
        db = connect_to_db()
        with db.cursor(pymysql.cursors.DictCursor) as cursor:
            # Fetch the user’s internal ID
            cursor.execute(
                "SELECT id FROM users WHERE phone=%s AND LOWER(fullname)=LOWER(%s)",
                (phone, fullname)
            )
            user = cursor.fetchone()
            if not user:
                log_event("transaction_user_not_found", phone, fullname)
                return jsonify({"error": "User not found"}), 404

            # Insert the new transaction
            cursor.execute(
                "INSERT INTO transactions (id, type, month, amount, date) "
                "VALUES (%s, %s, %s, %s, CURDATE())",
                (user['id'], transaction_type, months or '', amount)
            )
        db.commit()
    except pymysql.MySQLError as e:
        log_event("payment_insert_fail", phone, str(e))
        return jsonify({"error": "Transaction failed"}), 500
    finally:
        if db:
            db.close()

    return jsonify({"message": "Payment recorded"}), 200
