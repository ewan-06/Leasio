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
            return {"valuation": f"{result[0]:,.2f}"}
        else:
            return {"error": "No valuation found."}
            
    except Exception as e:
        return {"error": str(e)}
