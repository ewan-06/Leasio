from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import pymssql 
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Temporarily allows all origins so testing works anywhere
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 1. THE CALCULATOR ENDPOINT
@app.get("/api/calculate")
async def calculate_monthly(cap_code: str, term: int, mileage: int, initial: int):
    try:
        db_password = os.getenv("AZURE_SQL_PASSWORD", "")
        conn = pymssql.connect(
            server="leasio-server.database.windows.net",
            user="ewan_leasio",
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
        db_password = os.getenv("AZURE_SQL_PASSWORD", "")
        conn = pymssql.connect(
            server="leasio-server.database.windows.net",
            user="ewan_leasio",
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
async def get_best_offers():
    try:
        db_password = os.getenv("AZURE_SQL_PASSWORD", "")
        conn = pymssql.connect(
            server="leasio-server.database.windows.net",
            user="ewan_leasio",
            password=db_password,
            database="Leasio_database"
        )
        
        cursor = conn.cursor()
        
        # 1. Groups by Make/Model and ranks by Monthly/OTR
        # 2. Joins to Conduit to grab the Derivative (trim name)
        # NOTE: If your Conduit trim column is called Description, change c.Derivative to c.Description!
        cursor.execute("""
            WITH RankedOffers AS (
                SELECT 
                    CAP_Code,
                    Make,
                    Model,
                    Monthly,
                    [Monthly/OTR],
                    ROW_NUMBER() OVER(PARTITION BY Make, Model ORDER BY [Monthly/OTR] ASC) as RankNum
                FROM dbo.BestOffers
            )
            SELECT TOP 6 
                r.Make, 
                r.Model, 
                c.CAP_Derivative, 
                r.Monthly 
            FROM RankedOffers r
            LEFT JOIN dbo.Conduit c ON r.CAP_Code = c.CAP_Code
            WHERE r.RankNum = 1
            ORDER BY r.[Monthly/OTR] ASC;
        """)
        
        results = cursor.fetchall()
        
        offers_list = []
        for row in results:
            offers_list.append({
                "make": row[0],
                "model": row[1],
                "derivative": row[2] if row[2] else "Standard Spec", # Fallback if trim is blank
                "price": row[3]
            })
            
        return {"offers": offers_list}
            
    except Exception as e:
        return {"error": str(e)}

# 3. THE FILTERABLE SEARCH ENDPOINT (Updated with Vehicle Type support)
@app.get("/api/search_vehicles")
async def search_vehicles(make: str = "Any make", fuel: str = "Any fuel type", model: str = "", vehicle_type: str = "car"):
    try:
        db_password = os.getenv("AZURE_SQL_PASSWORD", "")
        conn = pymssql.connect(
            server="leasio-server.database.windows.net",
            user="ewan_leasio",
            password=db_password,
            database="Leasio_database"
        )
        cursor = conn.cursor()
        
        # Choose the correct RV table based on vehicle type
        rv_table = "dbo.LCV_RVs" if vehicle_type.lower() == "vans" else "dbo.Car_RVs"

        sql = f"""
            SELECT TOP 30 
                c.Manufacturer, 
                c.Model, 
                c.CAP_Derivative, 
                c.Fuel, 
                c.CAP_Code,
                b.Monthly
            FROM dbo.Conduit c
            INNER JOIN {rv_table} rv ON c.CAP_Code = rv.CAP_Code
            LEFT JOIN dbo.BestOffers b ON c.CAP_Code = b.CAP_Code
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
            
        cursor.execute(sql, tuple(params))
        results = cursor.fetchall()
        
        vehicles_list = []
        for row in results:
            vehicles_list.append({
                "Make": row[0],
                "Model": row[1],
                "CAP_Derivative": row[2] if row[2] else "Standard Spec",
                "Fuel": row[3] if row[3] else "Electric",
                "CAP_Code": row[4],
                "Monthly": row[5] if row[5] else "POA"
            })
            
        conn.close()
        return vehicles_list
        
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/get_models")
async def get_models(make: str, vehicle_type: str = "car"):
    try:
        db_password = os.getenv("AZURE_SQL_PASSWORD", "")
        conn = pymssql.connect(
            server="leasio-server.database.windows.net",
            user="ewan_leasio",
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
        db_password = os.getenv("AZURE_SQL_PASSWORD", "")
        conn = pymssql.connect(
            server="leasio-server.database.windows.net",
            user="ewan_leasio",
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
        db_password = os.getenv("AZURE_SQL_PASSWORD", "")
        conn = pymssql.connect(
            server="leasio-server.database.windows.net",
            user="ewan_leasio",
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
