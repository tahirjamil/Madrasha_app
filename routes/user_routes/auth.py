from flask import request, jsonify
from . import user_routes
import pymysql.cursors
from werkzeug.security import generate_password_hash, check_password_hash
from pymysql.err import IntegrityError
import pymysql
from database import connect_to_db
from helpers import validate_fullname, validate_password, send_sms, format_phone_number, generate_code, check_code
from logger import log_event


# ========== Routes ==========
@user_routes.route("/register", methods=["POST"])
def register():
    conn = connect_to_db()

    data = request.get_json()
    fullname = data.get("fullname", "").strip()
    phone = data.get("phone")
    password = data.get("password")
    user_code = data.get("code")

    if not fullname or not phone or not password or not user_code:
        log_event("auth_missing_fields", phone, "Phone or fullname missing")
        return jsonify({"message": "All fields including code are required"}), 400

    formatted_phone = format_phone_number(phone)
    if not formatted_phone:
        return jsonify({"message": "Invalid phone number format"}), 400

    validate_code = check_code(user_code, formatted_phone)
    if validate_code:
        return validate_code

    hashed_password = generate_password_hash(password)
    with conn.cursor(pymysql.cursors.DictCursor) as cursor:
        try:
            cursor.execute(
                "INSERT INTO users (fullname, phone, password) VALUES (%s, %s, %s)",
                (fullname, formatted_phone, hashed_password)
            )
            conn.commit()

            cursor.execute(
                "SELECT id FROM users WHERE LOWER(fullname) = LOWER(%s) AND phone = %s",
                (fullname, formatted_phone)
            )
            result = cursor.fetchone()
            user_id = result['id'] if result else None
            conn.commit()

            cursor.execute(
                "SELECT * FROM people WHERE LOWER(name_en) = LOWER(%s) AND phone = %s",
                (fullname, formatted_phone)
            )
            people_result = cursor.fetchone()

            if people_result:
                cursor.execute(
                    "UPDATE people SET id = %s WHERE LOWER(name_en) = LOWER(%s) AND phone = %s",
                    (user_id, fullname, formatted_phone)
                )
                conn.commit()

            return jsonify({"success": "Registration successful"}), 201

        except IntegrityError:
            return jsonify({"message": "User with this fullname and phone already registered"}), 400
        except Exception as e:
            return jsonify({"message": "Internal server error", "error": str(e)}), 500


@user_routes.route("/login", methods=["POST"])
def login():
    conn = connect_to_db()

    data = request.get_json()
    fullname = data.get("fullname", "").strip()
    phone = data.get("phone")
    password = data.get("password")

    if not fullname or not phone or not password:
        log_event("auth_missing_fields", phone, "Phone or fullname missing")
        return jsonify({"message": "All fields are required"}), 400

    formatted_phone = format_phone_number(phone)
    if not formatted_phone:
        log_event("auth_invalid_phone", phone, "Invalid phone format")
        return jsonify({"message": "Invalid phone number format"}), 400

    try:
        with conn.cursor(cursor=pymysql.cursors.DictCursor) as cursor:
            cursor.execute(
                "SELECT id, password FROM users WHERE phone = %s AND LOWER(fullname) = LOWER(%s)",
                (formatted_phone, fullname)
            )
            user = cursor.fetchone()

            if not user:
                log_event("auth_user_not_found", formatted_phone, f"User {fullname} not found")
                return jsonify({"message": "User not found"}), 404
            
            if not check_password_hash(user["password"], password):
                log_event("auth_incorrect_password", formatted_phone, "Incorrect password")
                return jsonify({"message": "Incorrect password"}), 401
            
            cursor.execute(
                """
                SELECT u.id, u.fullname, u.phone, p.* 
                FROM users u
                LEFT JOIN people p ON p.id = u.id
                WHERE u.id = %s
                """,
                (user["id"],)
            )
            info = cursor.fetchone()

            if not info or not info.get("phone"):
                log_event("auth_additional_info_required", formatted_phone, "Missing profile info")
                return jsonify({"message": "Additional info required"}), 400
            
            info.pop("password", None)

            return jsonify({"success": "Login successful", "info": info}), 200
            
    except Exception as e:
        log_event("auth_error", formatted_phone, str(e))
        return jsonify({"message": "Internal server error"}), 500

    finally:
        conn.close()

@user_routes.route("/send_code", methods=["POST"])
def send_verification_code():
    conn = connect_to_db()

    SEND_LIMIT_PER_HOUR = 3
    data = request.get_json()
    phone = data.get("phone")
    fullname = data.get("fullname").strip()
    password = data.get("password")

    if not phone:
        return jsonify({"message": "Phone number required"}), 400

    formatted_phone = format_phone_number(phone)
    if not formatted_phone:
        return jsonify({"message": "Invalid phone number format"}), 400

    try:
        with conn.cursor(pymysql.cursors.DictCursor) as cursor:
            if fullname:
                ok, msg = validate_fullname(fullname)
                if not ok:
                    return jsonify({"message": msg}), 400
            if password:
                ok, msg = validate_password(password)
                if not ok:
                    return jsonify({"message": msg}), 400

                cursor.execute("SELECT * FROM users WHERE phone = %s AND LOWER(fullname) = LOWER(%s)", (formatted_phone, fullname))
                if cursor.fetchone():
                    return jsonify({"message": "User already registered"}), 409

            # Rate limit check
            cursor.execute("""
                SELECT COUNT(*) AS recent_count 
                FROM verifications 
                WHERE phone = %s AND created_at > NOW() - INTERVAL 1 HOUR
            """, (formatted_phone,))
            result = cursor.fetchone()
            count = result["recent_count"] if result else 0

            if count >= SEND_LIMIT_PER_HOUR:
                log_event("rate_limit_blocked", phone, "SMS send limit exceeded")
                return jsonify({"message": "Limit reached. Try again later."}), 429


            # Send verification code
            code = generate_code()
            cursor.execute("INSERT INTO verifications (phone, code) VALUES (%s, %s)", (formatted_phone, code))
            conn.commit()

        if send_sms(formatted_phone, code):
            return jsonify({"success": f"Verification code sent to {formatted_phone}"}), 200
        else:
            return jsonify({"message": "Failed to send SMS"}), 500


    except Exception as e:
        return jsonify({"message": f"Internal error: {str(e)}"}), 500


@user_routes.route("/reset_password", methods=["POST"])
def reset_password():
    conn = connect_to_db()
    
    data = request.get_json()
    phone = data.get("phone")
    fullname = data.get("fullname").strip()
    user_code = data.get("code")
    old_password = data.get("old_password")
    new_password = data.get("new_password")

    # Check required fields (except old_password and code, because one of them must be present)
    if not all([phone, fullname, new_password]):
        log_event("auth_missing_fields", phone, "Phone or fullname missing")
        return jsonify({"message": "Phone, Fullname, and New Password are required"}), 400

    formatted_phone = format_phone_number(phone)
    if not formatted_phone:
        return jsonify({"message": "Invalid phone number format"}), 400

    # If old password is not provided, use code verification instead
    if not old_password:
        validate_code = check_code(user_code, formatted_phone)
        if validate_code:
            return validate_code

    # Fetch the user
    with conn.cursor(pymysql.cursors.DictCursor) as cursor:
        cursor.execute(
            "SELECT password FROM users WHERE phone = %s AND LOWER(fullname) = LOWER(%s)",
            (formatted_phone, fullname)
        )
        result = cursor.fetchone()

        if not result:
            return jsonify({"message": "User not found"}), 404

        # If old_password is given, check it
        if old_password:
            if not check_password_hash(result['password'], old_password):
                return jsonify({"message": "Incorrect old password"}), 401

        # Update the password
        hashed_password = generate_password_hash(new_password)
        cursor.execute(
            "UPDATE users SET password = %s WHERE LOWER(fullname) = LOWER(%s) AND phone = %s",
            (hashed_password, fullname, formatted_phone)
        )
        conn.commit()

    return jsonify({"message": "Password reset successful"}), 200
