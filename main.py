from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import List, Union
import os
import sqlite3
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from dotenv import load_dotenv
import uvicorn

# Cargamos el archivo .env
load_dotenv()

app = FastAPI()

# --- Configuración de rutas seguras ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
templates_path = os.path.join(BASE_DIR, "templates")
static_path = os.path.join(BASE_DIR, "static")

if not os.path.exists(static_path): 
    os.makedirs(static_path)
if not os.path.exists(templates_path):
    os.makedirs(templates_path)

app.mount("/static", StaticFiles(directory=static_path), name="static")
templates = Jinja2Templates(directory=templates_path)

# --- CONFIGURACIÓN DE SQLITE ---
DB_FILE = os.path.join(BASE_DIR, "glowlift.db")

def init_db():
    """Crea las tablas de la base de datos si no existen."""
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    
    # Habilitamos claves foráneas en SQLite
    cursor.execute("PRAGMA foreign_keys = ON;")
    
    # Tabla de Entrenamientos
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS entrenamientos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        rutina TEXT NOT NULL,
        fecha TEXT NOT NULL,
        timestamp REAL NOT NULL
    )
    """)
    
    # Tabla de Ejercicios
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS ejercicios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entrenamiento_id INTEGER,
        nombre TEXT NOT NULL,
        FOREIGN KEY (entrenamiento_id) REFERENCES entrenamientos(id) ON DELETE CASCADE
    )
    """)
    
    # Tabla de Series (kg es TEXT para soportar "BW", "B", etc.)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS series (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ejercicio_id INTEGER,
        kg TEXT NOT NULL,
        reps INTEGER NOT NULL,
        hecha INTEGER NOT NULL, -- 0 para falso, 1 para verdadero
        FOREIGN KEY (ejercicio_id) REFERENCES ejercicios(id) ON DELETE CASCADE
    )
    """)
    
    conn.commit()
    conn.close()

# Inicializamos la base de datos nada más arrancar
init_db()

# --- Modelos de Pydantic para validación ---
class SerieModel(BaseModel):
    kg: Union[float, int, str]  # Soporta números y texto tipo "BW"
    reps: int
    hecha: bool

class EjercicioModel(BaseModel):
    nombre: str
    series: List[SerieModel]

class EntrenamientoModel(BaseModel):
    rutina: str
    fecha: str
    timestamp: float
    ejercicios: List[EjercicioModel]


# --- RUTAS DE LA APP ---

@app.get("/", response_class=HTMLResponse)
def home():
    """Carga el index de manera directa y segura sin Jinja2."""
    ruta_html = os.path.join(templates_path, "index.html")
    if os.path.exists(ruta_html):
        with open(ruta_html, "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read())
    return HTMLResponse(content="<h1>Error: No se encuentra index.html</h1>", status_code=404)


@app.post("/api/guardar")
def guardar_en_sqlite(entrenamiento: EntrenamientoModel):
    """Guarda la sesión, sus ejercicios y series en la base de datos SQLite."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # 1. Insertar el entrenamiento principal
        cursor.execute(
            "INSERT INTO entrenamientos (rutina, fecha, timestamp) VALUES (?, ?, ?)",
            (entrenamiento.rutina, entrenamiento.fecha, entrenamiento.timestamp)
        )
        entrenamiento_id = cursor.lastrowid
        
        # 2. Insertar cada ejercicio de la sesión
        for ej in entrenamiento.ejercicios:
            cursor.execute(
                "INSERT INTO ejercicios (entrenamiento_id, nombre) VALUES (?, ?)",
                (entrenamiento_id, ej.nombre)
            )
            ejercicio_id = cursor.lastrowid
            
            # 3. Insertar cada serie de ese ejercicio
            for serie in ej.series:
                hecha_int = 1 if serie.hecha else 0
                cursor.execute(
                    "INSERT INTO series (ejercicio_id, kg, reps, hecha) VALUES (?, ?, ?, ?)",
                    (ejercicio_id, str(serie.kg), serie.reps, hecha_int)
                )
        
        conn.commit()
        conn.close()
        return {"status": "ok", "message": "Entrenamiento guardado en la base de datos SQLite con éxito."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error en la BD: {str(e)}")


@app.get("/api/historial")
def obtener_historial_sqlite():
    """Recupera todo el historial desde SQLite y le da formato JSON para el navegador."""
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        
        # Obtenemos todos los entrenamientos
        cursor.execute("SELECT id, rutina, fecha, timestamp FROM entrenamientos ORDER BY timestamp DESC")
        entrenamientos_raw = cursor.fetchall()
        
        historial = []
        
        for ent_id, rutina, fecha, timestamp in entrenamientos_raw:
            entrenamiento_dict = {
                "rutina": rutina,
                "fecha": fecha,
                "timestamp": timestamp,
                "ejercicios": []
            }
            
            # Buscamos los ejercicios de este entrenamiento
            cursor.execute("SELECT id, nombre FROM ejercicios WHERE entrenamiento_id = ?", (ent_id,))
            ejercicios_raw = cursor.fetchall()
            
            for ej_id, nombre_ej in ejercicios_raw:
                ejercicio_dict = {
                    "nombre": nombre_ej,
                    "series": []
                }
                
                # Buscamos las series de este ejercicio
                cursor.execute("SELECT kg, reps, hecha FROM series WHERE ejercicio_id = ?", (ej_id,))
                series_raw = cursor.fetchall()
                
                for kg, reps, hecha in series_raw:
                    # Intentamos convertir kg a flotante si es puramente un número para mantener el formato numérico original
                    try:
                        kg_valor = float(kg) if '.' in kg else int(kg)
                    except ValueError:
                        kg_valor = kg # Se queda como texto si es "BW", "60+B", etc.
                        
                    ejercicio_dict["series"].append({
                        "kg": kg_valor,
                        "reps": reps,
                        "hecha": True if hecha == 1 else False
                    })
                
                entrenamiento_dict["ejercicios"].append(ejercicio_dict)
            
            historial.append(entrenamiento_dict)
            
        conn.close()
        return historial
    except Exception as e:
        return []


@app.post("/api/enviar-reporte")
def enviar_reporte_email():
    """Recopila los logs de SQLite y los envía por email."""
    # Obtenemos el historial directamente usando nuestra nueva lógica de BD
    historial = obtener_historial_sqlite()
    if not historial:
        return JSONResponse(status_code=400, content={"message": "No hay entrenamientos en la base de datos para enviar."})
    
    try:
        REMITENTE = "irenerecu@gmail.com" 
        PASSWORD = "dkme rvhl cvxr sdrt" 
        DESTINATARIO = "irenerecu@gmail.com" 

        cuerpo_html = """
        <html>
            <body style="font-family: Arial, sans-serif; background-color: #0f172a; color: #f8fafc; padding: 20px;">
                <h2 style="color: #38bdf8; border-bottom: 2px solid #334155; padding-bottom: 10px;">
                    🚀 GlowLift - Tu Reporte Semanal de Fuerza (SQLite)
                </h2>
                <p style="color: #94a3b8;">Aquí tienes el resumen de tus últimas sesiones registradas:</p>
        """
        
        # Agrupamos los últimos 5 entrenamientos
        for entreno in historial[:5]:
            cuerpo_html += f"""
            <div style="background-color: #1e293b; border-radius: 8px; padding: 15px; margin-bottom: 15px; border: 1px solid #334155;">
                <div style="display: flex; justify-content: space-between; font-weight: bold; margin-bottom: 10px;">
                    <span style="color: #f8fafc; font-size: 16px;">{entreno['rutina']}</span>
                    <span style="color: #94a3b8; font-size: 13px;">{entreno['fecha']}</span>
                </div>
            """
            for ej in entreno['ejercicios']:
                series_completadas = sum(1 for s in ej['series'] if s['hecha'])
                
                # Manejamos los pesos mixtos de forma segura para extraer el máximo
                pesos_numericos = []
                for s in ej['series']:
                    try:
                        pesos_numericos.append(float(s['kg']))
                    except ValueError:
                        pass # Ignoramos textos como "BW" para la matemática del máximo
                
                max_peso = max(pesos_numericos) if pesos_numericos else "N/A"
                cuerpo_html += f"""
                <p style="margin: 4px 0; color: #cbd5e1; font-size: 14px;">
                    • <strong>{ej['nombre']}</strong>: {series_completadas}/{len(ej['series'])} series completadas | Máx: {max_peso} kg
                </p>
                """
            cuerpo_html += "</div>"
            
        cuerpo_html += """
                <p style="font-size: 12px; color: #64748b; margin-top: 20px; text-align: center;">
                    GlowLift Assistant • Impulsado de forma segura por SQLite
                </p>
            </body>
        </html>
        """

        msg = MIMEMultipart()
        msg['From'] = REMITENTE
        msg['To'] = DESTINATARIO
        msg['Subject'] = "📊 GlowLift - Resumen de Entrenamientos Semanal"
        msg.attach(MIMEText(cuerpo_html, 'html'))

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(REMITENTE, PASSWORD)
        server.sendmail(REMITENTE, DESTINATARIO, msg.as_string())
        server.quit()

        return {"status": "ok", "message": "Reporte enviado con éxito por email."}
    
    except Exception as e:
        return JSONResponse(status_code=500, content={"message": f"Error al enviar correo: {str(e)}"})
    
  if __name__ == "__main__":
    # Lee el puerto que asigna el servidor (Render) o usa el 8000 por defecto en local
    puerto = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=puerto)