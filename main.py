from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
from contextlib import asynccontextmanager
import asyncio
import time
import pymssql 
import os
import bcrypt

VEHICLE_CACHE = {
    "car": [],
    "vans": [],
    "last_updated": 0
}

CACHE_REFRESH_SECONDS = 900  # Refresh every 15 minutes

def fetch_vehicles_from_azure():
    db_password = os.getenv("SQL_USER_PASSWORD", "")
    conn = pymssql.connect(
        server="leasio-server.database.windows.net",
        user="leasio_user@leasio-server",
        password=db_password,
        database="Leasio_database"
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
                "_bodytype": (r[8] or "").strip().lower()
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
    await refresh_cache_once()
    task = asyncio.create_task(periodic_cache_worker())
    yield
    task.cancel()

app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://www.leasio.co.uk",
        "https://leasio.co.uk",
        "http://localhost:5500",
        "*"
    ],
    allow_credentials=False, # Set to False when using "*" or if not sending cookies
    allow_methods=["*"],
    allow_headers=["*"],
)

# 1. THE CALCULATOR ENDPOINT
@app.get("/api/calculate")
async def calculate_monthly(cap_code: str, term: int, mileage: int, initial: int):
    try:
        db_password = os.getenv("SQL_USER_PASSWORD", "")
        conn = pymssql.connect(
            server="leasio-server.database.windows.net",
            user="leasio_user@leasio-server",
            password=db_password,
            database="Leasio_database"
        )
        
        cursor = conn.cursor()
        cursor.execute("SELECT dbo.RentalWithFees(%s, %d, %d, %d)", 
                       (cap_code, term, mileage, initial))
        result = cursor.fetchone()
        
        if result and result[0] is not None:
            return {"valuation": f"£{result[0]:,.2f}"}
        else:
            return {"error": "No valuation found."}
            
    except Exception as e:
        return {"error": str(e)}

# 2. THE VEHICLE SEARCH ENDPOINT
@app.get("/api/get_types")
async def get_vehicle_types(make: str, model: str, fuel: str):
    try:
        db_password = os.getenv("SQL_USER_PASSWORD", "")
        conn = pymssql.connect(
            server="leasio-server.database.windows.net",
            user="leasio_user@leasio-server",
            password=db_password,
            database="Leasio_database"
        )
        
        cursor = conn.cursor()
        
        # NOTE: If your Conduit columns are named slightly differently 
        # (e.g., Manufacturer instead of Make), change c.Make, c.Model, etc. below!
        cursor.execute("""
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
        """, (make, model, fuel))
        
        results = cursor.fetchall()
        
        types_list = []
        for row in results:
            types_list.append({"cap_code": row[0], "type_name": row[1]})
            
        return {"types": types_list}
            
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/best_offers")
async def get_best_offers(vehicle_type: str = "car", limit: int = 9, max_monthly: float = 0):
    try:
        db_password = os.getenv("SQL_USER_PASSWORD", "")
        conn = pymssql.connect(
            server="leasio-server.database.windows.net",
            user="leasio_user@leasio-server",
            password=db_password,
            database="Leasio_database"
        )
        cursor = conn.cursor()
        
        rv_table = "dbo.LCV_RVs" if vehicle_type.lower() == "vans" else "dbo.Car_RVs"

        # Apply default smart price caps if none are explicitly requested:
        # Cars default to £400 max, Vans default to £800 max
        if max_monthly <= 0:
            max_monthly = 800.0 if vehicle_type.lower() == "vans" else 400.0

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
        
        offers = []
        for row in results:
            offers.append({
                "Make": row[0],
                "Model": row[1],
                "CAP_Derivative": row[2] if row[2] else "Standard Spec",
                "Fuel": row[3] if row[3] else "Electric",
                "CAP_Code": row[4],
                "Monthly": round(float(row[5]), 2) if row[5] else "POA"
            })
            
        conn.close()
        return offers
        
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
    limit: int = 24
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

    # Sort matching your exact SQL ORDER BY rules
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
        "vehicles": page_items
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

# 4. THE SINGLE VEHICLE LOOKUP ENDPOINT
@app.get("/api/vehicle")
async def get_vehicle(cap_code: str):
    try:
        db_password = os.getenv("SQL_USER_PASSWORD", "")
        conn = pymssql.connect(
            server="leasio-server.database.windows.net",
            user="leasio_user@leasio-server",
            password=db_password,
            database="Leasio_database"
        )
        
        cursor = conn.cursor()
        
        # Added the tech spec columns to the SELECT statement
        cursor.execute("""
            SELECT Manufacturer, Model, CAP_Derivative, Fuel, FLOOR(ModYr) AS ModYr, Eng_size, KW, BHP_code
            FROM dbo.Conduit 
            WHERE CAP_Code = %s
        """, (cap_code.strip(),))
        
        row = cursor.fetchone()
        conn.close()
        
        if row:
            # We safely format the tech specs here before sending them to the frontend
            return {
                "Make": row[0], 
                "Model": row[1], 
                "CAP_Derivative": row[2] if row[2] else "Standard Spec", 
                "Fuel": row[3] if row[3] else "Electric",
                "ModYr": str(row[4]) if row[4] else "",
                "EngineSize": f"{row[5]} cc" if row[5] else "N/A",
                "PowerKW": f"{row[6]} kW" if row[6] else "N/A",
                "BHP": str(row[7]) if row[7] else "N/A"
            }
        else:
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
        db_password = os.getenv("SQL_USER_PASSWORD", "")
        conn = pymssql.connect(
            server="leasio-server.database.windows.net",
            user="leasio_user@leasio-server",
            password=db_password,
            database="Leasio_database"
        )
        cursor = conn.cursor()
        
        # 1. Check if email or phone number already exists
        # Note: Since EmailEncrypted and PhoneEncrypted are stored via CAST(... AS VARBINARY(MAX)), 
        # we check against them by casting the incoming string or checking matching text representations.
        cursor.execute("""
            SELECT 1 FROM dbo.Customers 
            WHERE EmailEncrypted = CAST(%s AS VARBINARY(MAX))
               OR PhoneEncrypted = CAST(%s AS VARBINARY(MAX))
        """, (request.email.strip(), request.phone.strip()))
        
        existing_user = cursor.fetchone()
        if existing_user:
            conn.close()
            raise HTTPException(
                status_code=400, 
                detail="An account with this email address or phone number already exists."
            )
        
        # 2. Hash password & proceed with insertion if unique
        salt = bcrypt.gensalt()
        hashed_password = bcrypt.hashpw(request.password.encode('utf-8'), salt).decode('utf-8')
        
        cursor.execute("""
            INSERT INTO dbo.Customers (
                FirstName, LastName, DateOfBirth, 
                EmailEncrypted, PhoneEncrypted, AddressEncrypted, PasswordHash
            )
            VALUES (
                %s, %s, %s, 
                CAST(%s AS VARBINARY(MAX)), 
                CAST(%s AS VARBINARY(MAX)), 
                CAST(%s AS VARBINARY(MAX)), 
                %s
            )
        """, (
            request.first_name,
            request.last_name,
            str(request.date_of_birth),
            request.email.strip(),
            request.phone.strip(),
            request.address,
            hashed_password
        ))
        
        conn.commit()
        conn.close()
        return {"success": True, "message": "Account securely created."}
        
    except HTTPException:
        raise
    except Exception as e:
        print(f"Database error: {e}")
        raise HTTPException(status_code=400, detail=f"Registration failed: {str(e)}")

# Define the expected JSON payload for Login
class LoginRequest(BaseModel):
    email: str
    password: str

@app.post("/api/login")
def login_customer(request: LoginRequest):
    try:
        db_password = os.getenv("SQL_USER_PASSWORD", "")
        conn = pymssql.connect(
            server="leasio-server.database.windows.net",
            user="leasio_user@leasio-server",
            password=db_password,
            database="Leasio_database"
        )
        cursor = conn.cursor()
        
        # Look up the customer by matching the VARBINARY(MAX) cast used in /api/register
        cursor.execute("""
            SELECT FirstName, LastName, PasswordHash 
            FROM dbo.Customers 
            WHERE EmailEncrypted = CAST(%s AS VARBINARY(MAX))
        """, (request.email.strip(),))
        
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            raise HTTPException(status_code=401, detail="Invalid email or password.")
            
        first_name, last_name, stored_hash = row[0], row[1], row[2]
        
        # Ensure stored_hash is a string before encoding for bcrypt verification
        if isinstance(stored_hash, bytes):
            stored_hash_bytes = stored_hash
        else:
            stored_hash_bytes = str(stored_hash).encode('utf-8')
            
        # Verify the password with bcrypt
        if not bcrypt.checkpw(request.password.encode('utf-8'), stored_hash_bytes):
            raise HTTPException(status_code=401, detail="Invalid email or password.")
            
        return {
            "success": True,
            "message": "Login successful.",
            "user": {
                "first_name": first_name,
                "last_name": last_name,
                "email": request.email.strip()
            }
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
    initial: int = 9
    Initial: int | None = None
    price: float
    is_business: bool

@app.post("/api/save_quote")
def save_quote(req: QuoteRequest):
    try:
        db_password = os.getenv("SQL_USER_PASSWORD", "")
        conn = pymssql.connect(
            server="leasio-server.database.windows.net",
            user="leasio_user@leasio-server",
            password=db_password,
            database="Leasio_database"
        )
        cursor = conn.cursor()
        
        # 1. Look up CustomerID
        cursor.execute("""
            SELECT CustomerID 
            FROM dbo.Customers 
            WHERE EmailEncrypted = CAST(%s AS VARBINARY(MAX))
        """, (req.email.strip(),))
        
        user_row = cursor.fetchone()
        if not user_row:
            conn.close()
            raise HTTPException(status_code=404, detail="Customer not found.")
            
        customer_id = user_row[0]

        # 2. Get exact trimmed CAP code and values
        exact_cap = req.cap_code.strip()
        initial_val = req.Initial if req.Initial is not None else (req.initial or 9)
        price_val = float(req.price)
        business_val = 1 if req.is_business else 0
        
        # 3. Use %s universally for all parameters in pymssql
        cursor.execute("""
            INSERT INTO dbo.SavedQuotes (CustomerID, CAP_Code, Make, Model, Derivative, Term, Mileage, Initial, MonthlyRental, IsBusiness)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            customer_id, 
            exact_cap, 
            req.make, 
            req.model, 
            req.derivative, 
            int(req.term), 
            int(req.mileage), 
            int(initial_val), 
            price_val, 
            business_val
        ))
        
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
        db_password = os.getenv("SQL_USER_PASSWORD", "")
        conn = pymssql.connect(
            server="leasio-server.database.windows.net",
            user="leasio_user@leasio-server",
            password=db_password,
            database="Leasio_database"
        )
        cursor = conn.cursor()

        cursor.execute("""
            SELECT CustomerID 
            FROM dbo.Customers 
            WHERE EmailEncrypted = CAST(%s AS VARBINARY(MAX))
        """, (email.strip(),))
        user_row = cursor.fetchone()
        if not user_row:
            conn.close()
            return []

        customer_id = user_row[0]

        # Automatically purge quotes older than 30 days
        cursor.execute("""
            DELETE FROM dbo.SavedQuotes 
            WHERE CustomerID = %s 
              AND SavedAt < DATEADD(day, -30, SYSDATETIME())
        """, (customer_id,))
        conn.commit()

        # Fetch remaining active quotes
        cursor.execute("""
            SELECT QuoteID, CAP_Code, Make, Model, Derivative, Term, Mileage, Initial, MonthlyRental, IsBusiness, SavedAt
            FROM dbo.SavedQuotes 
            WHERE CustomerID = %s
            ORDER BY SavedAt DESC
        """, (customer_id,))

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
                "SavedAt": r[10].isoformat() if r[10] else None
            }
            for r in rows
        ]
    except Exception as e:
        print(f"Get Quotes Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/delete_quote")
def delete_quote(req: DeleteQuoteRequest):
    try:
        db_password = os.getenv("SQL_USER_PASSWORD", "")
        conn = pymssql.connect(
            server="leasio-server.database.windows.net",
            user="leasio_user@leasio-server",
            password=db_password,
            database="Leasio_database"
        )
        cursor = conn.cursor()

        cursor.execute("""
            SELECT CustomerID 
            FROM dbo.Customers 
            WHERE EmailEncrypted = CAST(%s AS VARBINARY(MAX))
        """, (req.email.strip(),))
        user_row = cursor.fetchone()
        if not user_row:
            conn.close()
            raise HTTPException(status_code=404, detail="Customer not found.")

        customer_id = user_row[0]

        cursor.execute("""
            DELETE FROM dbo.SavedQuotes 
            WHERE QuoteID = %s AND CustomerID = %s
        """, (req.quote_id, customer_id))

        conn.commit()
        conn.close()
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        print(f"Delete Quote Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
        
class EnquiryRequest(BaseModel):
    first_name: str
    last_name: str
    phone: str
    email: str
    agreed_terms: bool
    cap_code: str = ""
    vehicle_type: str = "car"

@app.post("/api/enquire")
def submit_enquiry(req: EnquiryRequest):
    if not req.agreed_terms:
        raise HTTPException(status_code=400, detail="You must agree to the terms and conditions.")
    # Optional: insert into a dbo.Enquiries table in Azure SQL using pymssql and %s placeholders
    return {"success": True, "message": "Enquiry received."}
