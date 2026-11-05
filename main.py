from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr
from sqlalchemy import text
import pymssql 
import os
import bcrypt

app = FastAPI()

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
        cursor.execute("SELECT dbo.MonthlyRentalFunc(%s, %d, %d, %d)", 
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

# 3. THE FILTERABLE SEARCH ENDPOINT (Updated with Max Price, Sorting, Size & Body Type)
@app.get("/api/search_vehicles")
async def search_vehicles(
    make: str = "Any make", 
    fuel: str = "Any fuel type", 
    model: str = "", 
    vehicle_type: str = "car",
    sort: str = "",
    max_price: float = 0,
    size: str = "",        # NEW: Accepts size parameter
    bodytype: str = ""     # NEW: Accepts bodytype parameter
):
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

        # Configure sorting based on user selection
        if sort == "price_asc":
            order_clause = "ORDER BY CASE WHEN TRY_CAST(b.Monthly AS FLOAT) IS NULL THEN 1 ELSE 0 END, TRY_CAST(b.Monthly AS FLOAT) ASC"
        elif sort == "price_desc":
            order_clause = "ORDER BY TRY_CAST(b.Monthly AS FLOAT) DESC"
        else:
            # DEFAULT / RELEVANCE: Sort by Best Value ([Monthly/OTR] scale)
            order_clause = "ORDER BY CASE WHEN TRY_CAST(b.[Monthly/OTR] AS FLOAT) IS NULL OR TRY_CAST(b.[Monthly/OTR] AS FLOAT) <= 0 THEN 1 ELSE 0 END, TRY_CAST(b.[Monthly/OTR] AS FLOAT) ASC"

        # NEW: Added LEFT JOIN for dbo.vehicle_types (vt)
        sql = f"""
            SELECT 
                c.Manufacturer, 
                c.Model, 
                c.CAP_Derivative, 
                c.Fuel, 
                c.CAP_Code,
                b.Monthly
            FROM dbo.Conduit c
            INNER JOIN {rv_table} rv ON c.CAP_Code = rv.CAP_Code
            LEFT JOIN dbo.BestOffers b ON c.CAP_Code = b.CAP_Code
            LEFT JOIN dbo.vehicle_types vt 
                ON c.Manufacturer = vt.Make 
                AND c.Model = vt.Model 
                AND c.CAP_Derivative = vt.Derivative
            WHERE 1=1
        """
        params = []
        
        if make and make != "Any make":
            sql += " AND LOWER(c.Manufacturer) = LOWER(%s)"
            params.append(make.strip())
            
        if fuel and fuel != "Any fuel type":
            sql += " AND LOWER(c.Fuel) = LOWER(%s)"
            params.append(fuel.strip())
            
        if model:
            sql += " AND LOWER(c.Model) LIKE LOWER(%s)"
            params.append(f"%{model.strip()}%")

        # Filter by max monthly price if set below the 2000 ceiling
        if max_price and max_price > 0 and max_price < 2000:
            sql += " AND TRY_CAST(b.Monthly AS FLOAT) IS NOT NULL AND TRY_CAST(b.Monthly AS FLOAT) <= %s"
            params.append(float(max_price))

        # NEW: Filter by Size
        if size:
            sql += " AND LOWER(vt.size) = LOWER(%s)"
            params.append(size.strip())

        # NEW: Filter by Body Type
        if bodytype:
            sql += " AND LOWER(vt.bodytype) = LOWER(%s)"
            params.append(bodytype.strip())

        sql += f" {order_clause}"
            
        cursor.execute(sql, tuple(params))
        results = cursor.fetchall()
        
        vehicles_list = []
        for row in results:
            vehicles_list.append({
                "Manufacturer": row[0],
                "Model": row[1],
                "CAP_Derivative": row[2] if row[2] else "Standard Spec",
                "Fuel": row[3] if row[3] else "Electric",
                "CAP_Code": row[4],
                "Monthly": round(float(row[5]), 2) if row[5] else "POA"
            })
            
        conn.close()
        return vehicles_list
        
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/get_models")
async def get_models(make: str, vehicle_type: str = "car"):
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

        cursor.execute(f"""
            SELECT DISTINCT c.Model 
            FROM dbo.Conduit c
            INNER JOIN {rv_table} rv ON c.CAP_Code = rv.CAP_Code
            WHERE LOWER(c.Manufacturer) = LOWER(%s)
            ORDER BY c.Model ASC
        """, (make.strip(),))
        
        results = cursor.fetchall()
        models_list = [row[0] for row in results if row[0]]
        
        conn.close()
        return {"models": models_list}
        
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/get_fuels")
async def get_fuels(make: str = "", model: str = "", vehicle_type: str = "car"):
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

        sql = f"""
            SELECT DISTINCT c.Fuel 
            FROM dbo.Conduit c
            INNER JOIN {rv_table} rv ON c.CAP_Code = rv.CAP_Code
            WHERE c.Fuel IS NOT NULL AND c.Fuel != ''
        """
        params = []
        
        if make and make != "Any make":
            sql += " AND LOWER(c.Manufacturer) = LOWER(%s)"
            params.append(make.strip())
            
        if model:
            sql += " AND LOWER(c.Model) = LOWER(%s)"
            params.append(model.strip())
            
        sql += " ORDER BY c.Fuel ASC"
        
        cursor.execute(sql, tuple(params))
        results = cursor.fetchall()
        
        fuels_list = [row[0] for row in results if row[0]]
        
        conn.close()
        return {"fuels": fuels_list}
        
    except Exception as e:
        return {"error": str(e)}

# 4. THE SINGLE VEHICLE LOOKUP ENDPOINT
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
            SELECT Manufacturer, Model, CAP_Derivative, Fuel, Eng_size, KW, BHP_code
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
                "EngineSize": f"{row[4]} cc" if row[4] else "N/A",
                "PowerKW": f"{row[5]} kW" if row[5] else "N/A",
                "BHP": str(row[6]) if row[6] else "N/A"
            }
        else:
            return {"error": "Vehicle not found"}
            
    except Exception as e:
        return {"error": str(e)}

@app.get("/api/get_makes")
async def get_makes(vehicle_type: str = "car"):
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

        cursor.execute(f"""
            SELECT DISTINCT c.Manufacturer 
            FROM dbo.Conduit c
            INNER JOIN {rv_table} rv ON c.CAP_Code = rv.CAP_Code
            WHERE c.Manufacturer IS NOT NULL AND c.Manufacturer != ''
            ORDER BY c.Manufacturer ASC
        """)
        
        results = cursor.fetchall()
        makes_list = [row[0] for row in results if row[0]]
        
        conn.close()
        return {"makes": makes_list}
        
    except Exception as e:
        return {"error": str(e)}

# Define the expected JSON payload from the frontend
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
