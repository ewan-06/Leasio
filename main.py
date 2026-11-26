from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional
from cryptography.fernet import Fernet, InvalidToken
import asyncio
import hashlib
import hmac
import os
import secrets
import time
import pymssql
import bcrypt
import requests

EMAIL_API_KEY = os.getenv("EMAIL_API_KEY", "")
NOREPLY_FROM = os.getenv("NOREPLY_FROM_EMAIL", "Leasio <noreply@leasio.co.uk>")
ENQUIRIES_TO = os.getenv("ENQUIRIES_TO_EMAIL", "enquiries@leasio.co.uk")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://leasio.co.uk")
PII_ENCRYPTION_KEY = os.getenv("PII_ENCRYPTION_KEY", "").strip()

_fernet = Fernet(PII_ENCRYPTION_KEY.encode("utf-8")) if PII_ENCRYPTION_KEY else None


def encrypt_pii(value: str) -> bytes:
    """Encrypts sensitive customer fields (email, phone, address) into UTF-8 bytes."""
    clean = (value or "").strip()
    if not clean:
        return b""
    if _fernet:
        return _fernet.encrypt(clean.encode("utf-8"))
    # Fallback if PII_ENCRYPTION_KEY is not configured yet
    return clean.encode("utf-16-le")


def encrypt_pii(value: str):
    """Encrypts sensitive customer fields (email, phone, address) for VARBINARY(MAX)."""
    clean = (value or "").strip()
    if not clean:
        return pymssql.Binary(b"")
    if _fernet:
        return pymssql.Binary(_fernet.encrypt(clean.encode("utf-8")))
    return pymssql.Binary(clean.encode("utf-16-le"))


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def email_lookup_digest(email: str) -> str:
    key_bytes = (PII_ENCRYPTION_KEY or "leasio-fallback-key").encode("utf-8")
    return hmac.new(key_bytes, normalize_email(email).encode("utf-8"), hashlib.sha256).hexdigest()


def find_customer_by_email(cursor, email: str):
    """Finds a customer row matching email across both legacy UTF-16LE and Fernet-encrypted rows."""
    target = normalize_email(email)
    if not target:
        return None

    # Fast path for legacy UTF-16LE rows
    cursor.execute(
        """
        SELECT
            CAST(CustomerID AS VARCHAR(36)),
            FirstName,
            LastName,
            EmailEncrypted,
            PhoneEncrypted,
            AddressEncrypted,
            PasswordHash
        FROM dbo.Customers
        WHERE EmailEncrypted = CAST(%s AS VARBINARY(MAX))
        """,
        (email.strip(),),
    )
    row = cursor.fetchone()
    if row:
        return row

    # Decrypts rows to match Fernet-encrypted emails
    cursor.execute(
        """
        SELECT
            CAST(CustomerID AS VARCHAR(36)),
            FirstName,
            LastName,
            EmailEncrypted,
            PhoneEncrypted,
            AddressEncrypted,
            PasswordHash
        FROM dbo.Customers
        """
    )
    for candidate in cursor.fetchall():
        if normalize_email(decrypt_pii(candidate[3])) == target:
            return candidate
    return None


def get_db_connection():
    db_password = os.getenv("SQL_USER_PASSWORD", "")
    return pymssql.connect(
        server="leasio-server.database.windows.net",
        user="leasio_user@leasio-server",
        password=db_password,
        database="Leasio_database",
    )


def send_email(to: str, subject: str, html: str, reply_to: str | None = None, text: str | None = None):
    payload = {
        "from": NOREPLY_FROM,
        "to": [to],
        "subject": subject,
        "html": html,
    }
    if text:
        payload["text"] = text
    if reply_to:
        payload["reply_to"] = reply_to

    res = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {EMAIL_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=10,
    )
    if not res.ok:
        print(f"Resend API error ({res.status_code}): {res.text}")
    res.raise_for_status()


def hash_reset_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


VEHICLE_CACHE = {
    "car": [],
    "vans": [],
    "last_updated": 0,
}

CACHE_REFRESH_SECONDS = 900  # Refresh every 15 minutes


def fetch_vehicles_from_azure():
    db_password = os.getenv("SQL_USER_PASSWORD", "")
    conn = pymssql.connect(
        server="leasio-server.database.windows.net",
        user="leasio_user@leasio-server",
        password=db_password,
        database="Leasio_database",
    )
    cursor = conn.cursor()

    def load_for_type(rv_table):
        cursor.execute(f"""
            SELECT
                c.Manufacturer,
                c.Model,
                c.CAP_Derivative,
                c.Fuel,
                c.CAP_Code,
                TRY_CAST(b.Monthly AS FLOAT) AS MonthlyFloat,
                TRY_CAST(b.[Monthly/OTR] AS FLOAT) AS ScoreFloat,
                vt.size,
                vt.bodytype
            FROM dbo.Conduit c
            INNER JOIN {rv_table} rv ON c.CAP_Code = rv.CAP_Code
            LEFT JOIN dbo.BestOffers b ON c.CAP_Code = b.CAP_Code
            LEFT JOIN dbo.vehicle_types vt
                ON c.Manufacturer = vt.Make
                AND c.Model = vt.Model
                AND c.CAP_Derivative = vt.Derivative
        """)
        rows = cursor.fetchall()
        items = []
        for r in rows:
            monthly_val = round(float(r[5]), 2) if r[5] is not None else None
            score_val = float(r[6]) if (r[6] is not None and r[6] > 0) else None
            items.append({
                "Make": r[0] or "",
                "Model": r[1] or "",
                "CAP_Derivative": r[2] if r[2] else "Standard Spec",
                "Fuel": r[3] if r[3] else "Electric",
                "CAP_Code": r[4],
                "Monthly": monthly_val if monthly_val is not None else "POA",
                "_monthly_num": monthly_val,
                "_score": score_val,
                "_size": (r[7] or "").strip().lower(),
                "_bodytype": (r[8] or "").strip().lower(),
            })
        return items

    cars = load_for_type("dbo.Car_RVs")
    vans = load_for_type("dbo.LCV_RVs")
    conn.close()
    return cars, vans


async def refresh_cache_once():
    try:
        cars, vans = await asyncio.to_thread(fetch_vehicles_from_azure)
        VEHICLE_CACHE["car"] = cars
        VEHICLE_CACHE["vans"] = vans
        VEHICLE_CACHE["last_updated"] = time.time()
        print(f"Cache refreshed: {len(cars)} cars, {len(vans)} vans")
    except Exception as e:
        print(f"Cache refresh failed: {e}")


async def periodic_cache_worker():
    while True:
        await asyncio.sleep(CACHE_REFRESH_SECONDS)
        await refresh_cache_once()


@asynccontextmanager
async def lifespan(app: FastAPI):
    warm_task = asyncio.create_task(refresh_cache_once())
    periodic_task = asyncio.create_task(periodic_cache_worker())
    try:
        yield
    finally:
        warm_task.cancel()
        periodic_task.cancel()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://www.leasio.co.uk",
        "https://leasio.co.uk",
        "http://localhost:5500",
        "*",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/calculate")
async def calculate_monthly(cap_code: str, term: int, mileage: int, initial: int):
    try:
        db_password = os.getenv("SQL_USER_PASSWORD", "")
        conn = pymssql.connect(
            server="leasio-server.database.windows.net",
            user="leasio_user@leasio-server",
            password=db_password,
            database="Leasio_database",
        )
        cursor = conn.cursor()
        cursor.execute(
            "SELECT dbo.RentalWithFees(%s, %s, %s, %s)",
            (cap_code.strip(), int(term), int(mileage), int(initial)),
        )
        result = cursor.fetchone()
        conn.close()

        if result and result[0] is not None:
            return {"valuation": f"£{result[0]:,.2f}"}
        return {"error": "No valuation found."}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/get_types")
async def get_vehicle_types(make: str, model: str, fuel: str):
    try:
        db_password = os.getenv("SQL_USER_PASSWORD", "")
        conn = pymssql.connect(
            server="leasio-server.database.windows.net",
            user="leasio_user@leasio-server",
            password=db_password,
            database="Leasio_database",
        )
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT c.CAP_Code, c.CAP_Derivative
            FROM dbo.Conduit c
            WHERE c.Manufacturer = %s
              AND c.Model = %s
              AND c.Fuel = %s
              AND (
                  EXISTS (SELECT 1 FROM dbo.Car_RVs rv WHERE rv.CAP_Code = c.CAP_Code)
                  OR
                  EXISTS (SELECT 1 FROM dbo.LCV_RVs lcv WHERE lcv.CAP_Code = c.CAP_Code)
              )
            """,
            (make, model, fuel),
        )
        results = cursor.fetchall()
        conn.close()
        return {"types": [{"cap_code": row[0], "type_name": row[1]} for row in results]}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/best_offers")
async def get_best_offers(vehicle_type: str = "car", limit: int = 9, max_monthly: float = 0):
    v_type = "vans" if vehicle_type.lower() == "vans" else "car"

    if max_monthly <= 0:
        max_monthly = 800.0 if v_type == "vans" else 400.0

    cached_items = VEHICLE_CACHE.get(v_type, []) if "VEHICLE_CACHE" in globals() else []
    if cached_items:
        eligible = [
            v for v in cached_items
            if v.get("_monthly_num") is not None
            and v.get("_score") is not None
            and v["_score"] > 0
            and v["_monthly_num"] <= max_monthly
        ]

        best_per_model = {}
        for v in eligible:
            key = (str(v.get("Make", "")).lower(), str(v.get("Model", "")).lower())
            rank = (v["_score"], v["_monthly_num"])
            if key not in best_per_model or rank < best_per_model[key][0]:
                best_per_model[key] = (rank, v)

        top_offers = sorted(best_per_model.values(), key=lambda pair: pair[0])[:limit]

        return [
            {
                "Make": v.get("Make", ""),
                "Model": v.get("Model", ""),
                "CAP_Derivative": v.get("CAP_Derivative") or "Standard Spec",
                "Fuel": v.get("Fuel") or "Electric",
                "CAP_Code": v.get("CAP_Code", ""),
                "Monthly": round(float(v["_monthly_num"]), 2),
            }
            for _, v in top_offers
        ]

    try:
        db_password = os.getenv("SQL_USER_PASSWORD", "")
        conn = pymssql.connect(
            server="leasio-server.database.windows.net",
            user="leasio_user@leasio-server",
            password=db_password,
            database="Leasio_database",
        )
        cursor = conn.cursor()
        rv_table = "dbo.LCV_RVs" if v_type == "vans" else "dbo.Car_RVs"

        sql = f"""
            WITH RankedOffers AS (
                SELECT
                    c.Manufacturer,
                    c.Model,
                    c.CAP_Derivative,
                    c.Fuel,
                    c.CAP_Code,
                    b.Monthly,
                    TRY_CAST(b.[Monthly/OTR] AS FLOAT) AS Score,
                    ROW_NUMBER() OVER (
                        PARTITION BY c.Manufacturer, c.Model
                        ORDER BY TRY_CAST(b.[Monthly/OTR] AS FLOAT) ASC, TRY_CAST(b.Monthly AS FLOAT) ASC
                    ) AS rn
                FROM dbo.BestOffers b
                INNER JOIN dbo.Conduit c ON b.CAP_Code = c.CAP_Code
                INNER JOIN {rv_table} rv ON c.CAP_Code = rv.CAP_Code
                WHERE b.Monthly IS NOT NULL
                  AND b.[Monthly/OTR] IS NOT NULL
                  AND TRY_CAST(b.[Monthly/OTR] AS FLOAT) > 0
                  AND TRY_CAST(b.Monthly AS FLOAT) <= %s
            )
            SELECT TOP (%s)
                Manufacturer,
                Model,
                CAP_Derivative,
                Fuel,
                CAP_Code,
                Monthly
            FROM RankedOffers
            WHERE rn = 1
            ORDER BY Score ASC
        """

        cursor.execute(sql, (max_monthly, limit))
        results = cursor.fetchall()
        conn.close()

        return [
            {
                "Make": row[0],
                "Model": row[1],
                "CAP_Derivative": row[2] if row[2] else "Standard Spec",
                "Fuel": row[3] if row[3] else "Electric",
                "CAP_Code": row[4],
                "Monthly": round(float(row[5]), 2) if row[5] else "POA",
            }
            for row in results
        ]
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/search_vehicles")
async def search_vehicles(
    make: str = "Any make",
    fuel: str = "Any fuel type",
    model: str = "",
    vehicle_type: str = "car",
    sort: str = "",
    max_price: float = 0,
    size: str = "",
    bodytype: str = "",
    page: int = 1,
    limit: int = 24,
):
    v_type = "vans" if vehicle_type.lower() == "vans" else "car"
    results = VEHICLE_CACHE.get(v_type, [])

    if make and make != "Any make":
        m_low = make.strip().lower()
        results = [v for v in results if v["Make"].lower() == m_low]

    if fuel and fuel != "Any fuel type":
        f_low = fuel.strip().lower()
        results = [v for v in results if v["Fuel"].lower() == f_low]

    if model:
        mod_low = model.strip().lower()
        results = [v for v in results if mod_low in v["Model"].lower()]

    if max_price and 0 < max_price < 2000:
        results = [v for v in results if v["_monthly_num"] is not None and v["_monthly_num"] <= float(max_price)]

    if size:
        s_low = size.strip().lower()
        results = [v for v in results if v["_size"] == s_low]

    if bodytype:
        b_low = bodytype.strip().lower()
        results = [v for v in results if v["_bodytype"] == b_low]

    if sort == "price_asc":
        results = sorted(results, key=lambda v: (v["_monthly_num"] is None, v["_monthly_num"] or 0))
    elif sort == "price_desc":
        results = sorted(results, key=lambda v: v["_monthly_num"] if v["_monthly_num"] is not None else -1, reverse=True)
    else:
        results = sorted(results, key=lambda v: (v["_score"] is None, v["_score"] or 0))

    total_count = len(results)
    offset = (page - 1) * limit
    page_items = [
        {k: v[k] for k in ("Make", "Model", "CAP_Derivative", "Fuel", "CAP_Code", "Monthly")}
        for v in results[offset:offset + limit]
    ]

    return {
        "total_count": total_count,
        "vehicles": page_items,
    }


@app.get("/api/get_models")
async def get_models(make: str, vehicle_type: str = "car"):
    v_type = "vans" if vehicle_type.lower() == "vans" else "car"
    m_low = make.strip().lower()
    items = VEHICLE_CACHE.get(v_type, [])
    models_list = sorted({v["Model"] for v in items if v["Model"] and v["Make"].lower() == m_low})
    return {"models": models_list}


@app.get("/api/get_fuels")
async def get_fuels(make: str = "", model: str = "", vehicle_type: str = "car"):
    v_type = "vans" if vehicle_type.lower() == "vans" else "car"
    items = VEHICLE_CACHE.get(v_type, [])
    if make and make != "Any make":
        m_low = make.strip().lower()
        items = [v for v in items if v["Make"].lower() == m_low]
    if model:
        mod_low = model.strip().lower()
        items = [v for v in items if v["Model"].lower() == mod_low]
    fuels_list = sorted({v["Fuel"] for v in items if v["Fuel"]})
    return {"fuels": fuels_list}


@app.get("/api/vehicle")
async def get_vehicle(cap_code: str):
    try:
        db_password = os.getenv("SQL_USER_PASSWORD", "")
        conn = pymssql.connect(
            server="leasio-server.database.windows.net",
            user="leasio_user@leasio-server",
            password=db_password,
            database="Leasio_database",
        )
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT Manufacturer, Model, CAP_Derivative, Fuel, Eng_size, KW, BHP_code,
                CAST(FLOOR(TRY_CAST(ModYr AS FLOAT)) AS INT) AS ModYr
            FROM dbo.Conduit
            WHERE CAP_Code = %s
            """,
            (cap_code.strip(),),
        )
        row = cursor.fetchone()
        conn.close()

        if row:
            return {
                "Make": row[0],
                "Model": row[1],
                "CAP_Derivative": row[2] if row[2] else "Standard Spec",
                "Fuel": row[3] if row[3] else "Electric",
                "EngineSize": f"{row[4]} cc" if row[4] else "N/A",
                "PowerKW": f"{row[5]} kW" if row[5] else "N/A",
                "BHP": str(row[6]) if row[6] else "N/A",
                "ModYr": str(row[7]) if row[7] else "",
            }
        return {"error": "Vehicle not found"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/get_makes")
async def get_makes(vehicle_type: str = "car"):
    v_type = "vans" if vehicle_type.lower() == "vans" else "car"
    items = VEHICLE_CACHE.get(v_type, [])
    makes_list = sorted({v["Make"] for v in items if v["Make"]})
    return {"makes": makes_list}


class RegisterRequest(BaseModel):
    first_name: str
    last_name: str
    date_of_birth: str
    email: str
    phone: str
    address: str
    password: str


@app.post("/api/register")
def register_customer(request: RegisterRequest):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        target_email = normalize_email(request.email)
        target_phone = request.phone.strip()

        cursor.execute(
            """
            SELECT EmailEncrypted, PhoneEncrypted
            FROM dbo.Customers
            """
        )
        for row in cursor.fetchall():
            existing_email = normalize_email(decrypt_pii(row[0]))
            existing_phone = decrypt_pii(row[1])
            if (target_email and existing_email == target_email) or (target_phone and existing_phone == target_phone):
                conn.close()
                raise HTTPException(
                    status_code=400,
                    detail="An account with this email address or phone number already exists.",
                )

        hashed_password = bcrypt.hashpw(request.password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        enc_email = encrypt_pii(request.email)
        enc_phone = encrypt_pii(request.phone)
        enc_address = encrypt_pii(request.address)

        cursor.execute(
            """
            INSERT INTO dbo.Customers (
                FirstName, LastName, DateOfBirth,
                EmailEncrypted, PhoneEncrypted, AddressEncrypted, PasswordHash
            )
            VALUES (
                %s, %s, %s,
                CONVERT(VARBINARY(MAX), %s),
                CONVERT(VARBINARY(MAX), %s),
                CONVERT(VARBINARY(MAX), %s),
                %s
            )
            """,
            (
                request.first_name.strip(),
                request.last_name.strip(),
                str(request.date_of_birth),
                enc_email,
                enc_phone,
                enc_address,
                hashed_password,
            ),
        )

        conn.commit()
        conn.close()
        return {"success": True, "message": "Account securely created."}

    except HTTPException:
        raise
    except Exception as e:
        print(f"Database error: {e}")
        raise HTTPException(status_code=400, detail=f"Registration failed: {str(e)}")


class LoginRequest(BaseModel):
    email: str
    password: str


@app.post("/api/login")
def login_customer(request: LoginRequest):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        row = find_customer_by_email(cursor, request.email)
        conn.close()

        if not row:
            raise HTTPException(status_code=401, detail="Invalid email or password.")

        first_name, last_name, stored_hash = row[1], row[2], row[6]
        stored_hash_bytes = stored_hash if isinstance(stored_hash, bytes) else str(stored_hash).encode("utf-8")

        if not bcrypt.checkpw(request.password.encode("utf-8"), stored_hash_bytes):
            raise HTTPException(status_code=401, detail="Invalid email or password.")

        return {
            "success": True,
            "message": "Login successful.",
            "user": {
                "first_name": first_name,
                "last_name": last_name,
                "email": request.email.strip(),
            },
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"Login error: {e}")
        raise HTTPException(status_code=500, detail="An error occurred during login.")


class QuoteRequest(BaseModel):
    email: str
    cap_code: str
    make: str
    model: str
    derivative: str
    term: int
    mileage: int
    initial: int = 12
    Initial: int | None = None
    price: float
    is_business: bool


@app.post("/api/save_quote")
def save_quote(req: QuoteRequest):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        user_row = find_customer_by_email(cursor, req.email)
        if not user_row:
            conn.close()
            raise HTTPException(status_code=404, detail="Customer not found.")

        customer_id = user_row[0]
        exact_cap = req.cap_code.strip()
        initial_val = req.Initial if req.Initial is not None else (req.initial or 12)
        price_val = float(req.price)
        business_val = 1 if req.is_business else 0

        cursor.execute(
            """
            INSERT INTO dbo.SavedQuotes (CustomerID, CAP_Code, Make, Model, Derivative, Term, Mileage, Initial, MonthlyRental, IsBusiness)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                customer_id,
                exact_cap,
                req.make,
                req.model,
                req.derivative,
                int(req.term),
                int(req.mileage),
                int(initial_val),
                price_val,
                business_val,
            ),
        )

        conn.commit()
        conn.close()
        return {"success": True}

    except HTTPException:
        raise
    except Exception as e:
        print(f"Save Quote Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class DeleteQuoteRequest(BaseModel):
    email: str
    quote_id: int


@app.get("/api/get_quotes")
def get_quotes(email: str):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        user_row = find_customer_by_email(cursor, email)
        if not user_row:
            conn.close()
            return []

        customer_id = user_row[0]

        cursor.execute(
            """
            DELETE FROM dbo.SavedQuotes
            WHERE CustomerID = %s
              AND SavedAt < DATEADD(day, -30, SYSDATETIME())
            """,
            (customer_id,),
        )
        conn.commit()

        cursor.execute(
            """
            SELECT QuoteID, CAP_Code, Make, Model, Derivative, Term, Mileage, Initial, MonthlyRental, IsBusiness, SavedAt
            FROM dbo.SavedQuotes
            WHERE CustomerID = %s
            ORDER BY SavedAt DESC
            """,
            (customer_id,),
        )

        rows = cursor.fetchall()
        conn.close()

        return [
            {
                "QuoteID": r[0],
                "CAP_Code": r[1].strip() if r[1] else "",
                "Make": r[2],
                "Model": r[3],
                "Derivative": r[4],
                "Term": r[5],
                "Mileage": r[6],
                "Initial": r[7],
                "MonthlyRental": float(r[8]) if r[8] is not None else 0.0,
                "IsBusiness": bool(r[9]),
                "SavedAt": r[10].isoformat() if r[10] else None,
            }
            for r in rows
        ]
    except Exception as e:
        print(f"Get Quotes Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/delete_quote")
def delete_quote(req: DeleteQuoteRequest):
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        user_row = find_customer_by_email(cursor, req.email)
        if not user_row:
            conn.close()
            raise HTTPException(status_code=404, detail="Customer not found.")

        customer_id = user_row[0]

        cursor.execute(
            """
            DELETE FROM dbo.SavedQuotes
            WHERE QuoteID = %s AND CustomerID = %s
            """,
            (req.quote_id, customer_id),
        )

        conn.commit()
        conn.close()
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Delete Quote Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class EnquiryRequest(BaseModel):
    is_logged_in: bool = False
    customer_id: Optional[str] = None
    first_name: str = ""
    last_name: str = ""
    email: str
    phone: str = ""
    address: str = ""
    cap_code: str
    vehicle_type: str = "car"
    make: str = ""
    model: str = ""
    derivative: str = ""
    term: int = 48
    mileage: int = 5000
    initial_months: int = 12
    initial_amount: str = ""
    monthly_price: str = ""
    vat_status: str = "inc VAT"
    agreed_terms: bool = True


@app.post("/api/enquire")
def submit_enquiry(req: EnquiryRequest):
    if not req.agreed_terms:
        raise HTTPException(status_code=400, detail="You must agree to the terms and conditions.")

    timestamp_uk = datetime.now(ZoneInfo("Europe/London")).strftime("%d %b %Y at %H:%M:%S %Z")

    customer_number = "Guest (Not signed in)"
    first_name = req.first_name.strip()
    last_name = req.last_name.strip()
    phone = req.phone.strip()
    address = req.address.strip()
    email = req.email.strip()

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        if req.is_logged_in and email:
            row = find_customer_by_email(cursor, email)
            if row:
                customer_number = str(row[0])
                first_name = row[1] or first_name
                last_name = row[2] or last_name
                decrypted_email = decrypt_pii(row[3])
                decrypted_phone = decrypt_pii(row[4])
                decrypted_address = decrypt_pii(row[5])
                email = decrypted_email or email
                phone = decrypted_phone or phone
                address = decrypted_address or address

        conn.close()
        conn = None

        html = f"""
        <h2>New Vehicle Lease Enquiry</h2>
        <p><strong>Received:</strong> {timestamp_uk}</p>
        <h3>Customer Details</h3>
        <ul>
          <li><strong>Customer Number:</strong> {customer_number}</li>
          <li><strong>Name:</strong> {first_name} {last_name}</li>
          <li><strong>Email:</strong> {email}</li>
          <li><strong>Phone:</strong> {phone}</li>
          <li><strong>Address:</strong> {address}</li>
        </ul>
        <h3>Vehicle &amp; Deal Summary</h3>
        <ul>
          <li><strong>Vehicle:</strong> {req.make} {req.model} — {req.derivative}</li>
          <li><strong>Type / CAP Code:</strong> {req.vehicle_type.upper()} / {req.cap_code}</li>
          <li><strong>Monthly Rental:</strong> {req.monthly_price}/mo ({req.vat_status})</li>
          <li><strong>Initial Rental:</strong> {req.initial_months} months ({req.initial_amount})</li>
          <li><strong>Contract Term:</strong> {req.term} months</li>
          <li><strong>Annual Mileage:</strong> {req.mileage:,} miles/yr</li>
        </ul>
        """

        send_email(
            to=ENQUIRIES_TO,
            subject=f"New Enquiry: {req.make} {req.model} ({req.monthly_price}/mo {req.vat_status})",
            html=html,
            reply_to=email if email else None,
        )
        return {"success": True, "message": "Enquiry received.", "timestamp": timestamp_uk}

    except HTTPException:
        raise
    except Exception as e:
        if conn:
            conn.close()
        print(f"Enquiry error: {e}")
        raise HTTPException(status_code=500, detail="Could not send enquiry. Please try again.")


class ForgotPasswordRequest(BaseModel):
    email: str


@app.post("/api/forgot-password")
def forgot_password(req: ForgotPasswordRequest):
    generic_msg = {"success": True, "message": "If an account exists for that email, a reset link has been sent."}
    email = req.email.strip()
    if not email:
        return generic_msg

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        row = find_customer_by_email(cursor, email)
        if not row:
            conn.close()
            return generic_msg

        customer_id = row[0]
        recipient_email = decrypt_pii(row[3]) or email
        raw_token = secrets.token_urlsafe(32)
        token_hash = hash_reset_token(raw_token)
        expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=30)

        cursor.execute(
            """
            UPDATE dbo.PasswordResets
            SET Used = 1
            WHERE CAST(CustomerID AS VARCHAR(36)) = %s AND Used = 0
            """,
            (customer_id,),
        )

        cursor.execute(
            """
            INSERT INTO dbo.PasswordResets (CustomerID, TokenHash, ExpiresAt, Used)
            VALUES (CAST(%s AS UNIQUEIDENTIFIER), %s, %s, 0)
            """,
            (customer_id, token_hash, expires_at),
        )
        conn.commit()
        conn.close()
        conn = None

        base_url = FRONTEND_URL.rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            base_url = f"https://{base_url}"

        reset_link = f"{base_url}/reset-password.html?token={raw_token}"
        html = f"""
        <h2>Reset Your Leasio Password</h2>
        <p>We received a request to reset the password for your Leasio account.</p>
        <p>
          <a href="{reset_link}" style="color: #2563eb; font-weight: 600; text-decoration: underline;">
            Click here to set a new password
          </a>
        </p>
        <br>
        <p>This link expires in 30 minutes. If you did not request a reset, you can safely ignore this email.</p>
        """
        plain_text = (
            "Reset Your Leasio Password\n\n"
            "We received a request to reset the password for your Leasio account.\n\n"
            f"Reset your password: {reset_link}\n\n"
            "This link expires in 30 minutes. If you did not request a reset, you can safely ignore this email."
        )

        send_email(
            to=recipient_email,
            subject="Reset your Leasio password",
            html=html,
            text=plain_text,
        )
        return generic_msg

    except Exception as e:
        if conn:
            conn.close()
        print(f"Forgot password error: {e}")
        raise HTTPException(status_code=500, detail="Could not process password reset request.")


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


@app.post("/api/reset-password")
def reset_password(req: ResetPasswordRequest):
    if not req.token or len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="Invalid token or password must be at least 8 characters.")

    token_hash = hash_reset_token(req.token.strip())
    now_utc = datetime.now(timezone.utc)

    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT TokenID, CAST(CustomerID AS VARCHAR(36)), ExpiresAt, Used
            FROM dbo.PasswordResets
            WHERE TokenHash = %s
            """,
            (token_hash,),
        )
        token_row = cursor.fetchone()

        if not token_row:
            conn.close()
            raise HTTPException(status_code=400, detail="Invalid or expired password reset link.")

        token_id, customer_id, expires_at, used = token_row[0], token_row[1], token_row[2], bool(token_row[3])

        if used:
            conn.close()
            raise HTTPException(status_code=400, detail="This password reset link has already been used.")

        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if now_utc > expires_at:
            conn.close()
            raise HTTPException(status_code=400, detail="This password reset link has expired.")

        new_hash = bcrypt.hashpw(req.new_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

        cursor.execute(
            """
            UPDATE dbo.Customers
            SET PasswordHash = %s
            WHERE CAST(CustomerID AS VARCHAR(36)) = %s
            """,
            (new_hash, customer_id),
        )

        cursor.execute(
            """
            UPDATE dbo.PasswordResets
            SET Used = 1
            WHERE TokenID = %s
            """,
            (token_id,),
        )

        conn.commit()
        conn.close()
        return {"success": True, "message": "Password updated successfully."}

    except HTTPException:
        raise
    except Exception as e:
        if conn:
            conn.close()
        print(f"Reset password error: {e}")
        raise HTTPException(status_code=500, detail="Could not reset password.")
