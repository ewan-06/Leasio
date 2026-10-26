from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import pyodbc
import os  # Added this to securely load passwords

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
        # Securely pull the password from the server environment
        db_password = os.getenv("AZURE_SQL_PASSWORD", "")
        
        conn = pyodbc.connect(
            "DRIVER={ODBC Driver 17 for SQL Server};"
            "SERVER=leasio-server.database.windows.net;"
            "DATABASE=Leasio_database;" 
            "UID=ewan_leasio;"
            f"PWD={{{db_password}}};" # Safely injects the password with curly braces
            "Encrypt=yes;"
            "TrustServerCertificate=no;"
            "Connection Timeout=30;"
        )
        
        cursor = conn.cursor()
        cursor.execute("SELECT dbo.MonthlyRentalFunc(?, ?, ?, ?)", 
                       (cap_code, term, mileage, initial))
        result = cursor.fetchone()
        
        if result and result[0] is not None:
            return {"valuation": f"{result[0]:,.2f}"}
        else:
            return {"error": "No valuation found."}
            
    except Exception as e:
        return {"error": str(e)}