from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import pymssql 
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://www.leasio.co.uk"], 
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
        
        # This SQL groups by Make & Model, ranks by Monthly/OTR, and only takes the #1 from each group.
        # NOTE: Check your BestOffers view in SSMS to ensure these exact column names exist!
        cursor.execute("""
            WITH RankedOffers AS (
                SELECT 
                    Make,
                    Model,
                    Derivative,
                    [Monthly/OTR],
                    MonthlyRental,
                    ROW_NUMBER() OVER(PARTITION BY Make, Model ORDER BY [Monthly/OTR] ASC) as RankNum
                FROM dbo.BestOffers
            )
            SELECT TOP 6 Make, Model, Derivative, MonthlyRental 
            FROM RankedOffers
            WHERE RankNum = 1
            ORDER BY [Monthly/OTR] ASC;
        """)
        
        results = cursor.fetchall()
        
        offers_list = []
        for row in results:
            offers_list.append({
                "make": row[0],
                "model": row[1],
                "derivative": row[2],
                "price": row[3]
            })
            
        return {"offers": offers_list}
            
    except Exception as e:
        return {"error": str(e)}
