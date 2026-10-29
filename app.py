
"""Mini sales BI assistant: stdlib WSGI, SQLite, and allow-listed analytics."""
from __future__ import annotations

import csv
import json
import re
import sqlite3
from calendar import month_name
from datetime import datetime
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parent
CSV_PATH = ROOT / "data" / "sales.csv"
DB_PATH = ROOT / "data" / "sales.sqlite3"
EXPECTED = ["Transaction_Id", "Order_date", "Product", "Category", "Region", "Sales_channel", "Quantity", "Unit_price", "Discount_amount", "Status"]


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def initialize(csv_path: Path = CSV_PATH, db_path: Path = DB_PATH) -> dict:
    """Create schema and import clean rows, replacing prior imported data."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sales (
          transaction_id TEXT PRIMARY KEY, order_date TEXT NOT NULL,
          product TEXT NOT NULL, category TEXT NOT NULL, region TEXT NOT NULL,
          sales_channel TEXT NOT NULL, quantity INTEGER NOT NULL CHECK(quantity > 0),
          unit_price REAL NOT NULL CHECK(unit_price > 0),
          discount_amount REAL NOT NULL CHECK(discount_amount >= 0),
          status TEXT NOT NULL,
          revenue REAL GENERATED ALWAYS AS (quantity * unit_price - discount_amount) VIRTUAL
        );
        CREATE INDEX IF NOT EXISTS idx_sales_date ON sales(order_date);
        CREATE TABLE IF NOT EXISTS import_audit (
          id INTEGER PRIMARY KEY CHECK(id=1), source_file TEXT, imported_rows INTEGER,
          excluded_rows INTEGER, completed_at TEXT
        );
    """)
    conn.execute("DELETE FROM sales")
    imported = excluded = 0
    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != EXPECTED:
            raise ValueError("CSV columns do not match the expected sales schema")
        for row in reader:
            try:
                txid = row["Transaction_Id"].strip()
                date = datetime.strptime(row["Order_date"].strip(), "%d-%m-%Y").date().isoformat()
                product, category, region = (row[k].strip() for k in ("Product", "Category", "Region"))
                channel, status = row["Sales_channel"].strip(), row["Status"].strip().title()
                qty, price, discount = int(row["Quantity"]), float(row["Unit_price"]), float(row["Discount_amount"])
                if not all((txid, product, category, region, channel, status)) or qty <= 0 or price <= 0 or discount < 0 or discount > qty * price:
                    raise ValueError("invalid or non-revenue row")
                if status != "Completed":
                    raise ValueError("non-completed transaction")
                conn.execute("INSERT INTO sales(transaction_id,order_date,product,category,region,sales_channel,quantity,unit_price,discount_amount,status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                             (txid, date, product, category, region, channel, qty, price, discount, status))
                imported += 1
            except (ValueError, TypeError, KeyError, sqlite3.IntegrityError):
                excluded += 1
    conn.execute("INSERT OR REPLACE INTO import_audit VALUES (1,?,?,?,?)",
                 (csv_path.name, imported, excluded, datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    conn.close()
    return {"imported_rows": imported, "excluded_rows": excluded}


def _month_ref(text: str) -> str | None:
    """Resolve named or YYYY-MM month mentions to an ISO month."""
    m = re.search(r"\b(20\d{2})[-/](0?[1-9]|1[0-2])\b", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    month = re.search(r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b(?:\s+(20\d{2}))?", text, re.I)
    if month:
        names = {month_name[i].lower()[:3]: i for i in range(1, 13)}
        year = month.group(2) or "2026"
        return f"{year}-{names[month.group(1).lower()[:3]]:02d}"
    return None


def interpret(question: str) -> tuple[str, list[str]]:
    """Map natural language into one of the supported, parameterized intents."""
    q = re.sub(r"\s+", " ", question.strip().lower())
    if not q or len(q) > 500:
        raise ValueError("Enter a question between 1 and 500 characters.")
    if any(word in q for word in ("drop table", "delete from", "update sales", "insert into", "truncate")):
        raise ValueError("Database-changing requests are not supported.")
    months = re.findall(r"\b20\d{2}[-/](?:0?[1-9]|1[0-2])\b", q)
    if "between" in q and len(months) >= 2:
        return "compare_months", [months[0].replace("/", "-")[:5] + months[0].replace("/", "-")[5:].zfill(2), months[1].replace("/", "-")[:5] + months[1].replace("/", "-")[5:].zfill(2)]
    named_month = re.search(r"\b(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b", q)
    if ("change" in q or "compare" in q or "between" in q) and ("month" in q or len(months) >= 2 or named_month):
        found = [_month_ref(x) for x in re.findall(r"[a-z]+(?:\s+20\d{2})?|20\d{2}[-/]\d{1,2}", q)]
        found = [m for m in found if m]
        if len(found) >= 2:
            return "compare_months", found[:2]
        raise ValueError("For a month comparison, specify two months, such as 'January 2026 and February 2026'.")
    if "month" in q and any(k in q for k in ("trend", "monthly", "each", "over time")):
        return "monthly_trend", []
    if any(k in q for k in ("region", "territory")) and any(k in q for k in ("highest", "top", "best", "most", "sales")):
        return "top_region", []
    if any(k in q for k in ("product", "item")) and any(k in q for k in ("highest", "top", "best", "most", "generated")):
        return "top_product", []
    if any(k in q for k in ("total revenue", "revenue", "sales total", "overall sales")):
        return "total_revenue", []
    raise ValueError("I can answer total revenue, top product by revenue, monthly revenue trend, top region by sales, or revenue change between two months.")


QUERIES = {
    "total_revenue": "SELECT ROUND(SUM(revenue),2) AS value, COUNT(*) AS transactions, SUM(quantity) AS units FROM sales",
    "top_product": "SELECT product AS label, ROUND(SUM(revenue),2) AS revenue, SUM(quantity) AS units FROM sales GROUP BY product ORDER BY revenue DESC, product LIMIT 1",
    "monthly_trend": "SELECT strftime('%Y-%m',order_date) AS month, ROUND(SUM(revenue),2) AS revenue, COUNT(*) AS transactions FROM sales GROUP BY month ORDER BY month",
    "top_region": "SELECT region AS label, ROUND(SUM(revenue),2) AS revenue, COUNT(*) AS transactions FROM sales GROUP BY region ORDER BY revenue DESC, region LIMIT 1",
    "compare_months": "SELECT strftime('%Y-%m',order_date) AS month, ROUND(SUM(revenue),2) AS revenue, COUNT(*) AS transactions FROM sales WHERE strftime('%Y-%m',order_date) IN (?,?) GROUP BY month ORDER BY month",
}
LABELS = {"total_revenue":"Total revenue", "top_product":"Top product by revenue", "monthly_trend":"Monthly revenue trend", "top_region":"Top region by sales", "compare_months":"Revenue change between months"}


def answer(question: str, db_path: Path = DB_PATH) -> dict:
    intent, params = interpret(question)
    conn = connect(db_path)
    conn.execute("PRAGMA query_only = ON")
    rows = [dict(r) for r in conn.execute(QUERIES[intent], params)]
    conn.close()
    if intent == "compare_months":
        by_month = {r["month"]: r for r in rows}
        rows = [by_month.get(m, {"month":m,"revenue":0,"transactions":0}) for m in params]
        delta = round(rows[1]["revenue"] - rows[0]["revenue"], 2)
        pct = round(delta / rows[0]["revenue"] * 100, 2) if rows[0]["revenue"] else None
        result = {"months":rows, "change":delta, "change_percent":pct}
        summary = f"Revenue { 'increased' if delta >= 0 else 'decreased' } by ₹{abs(delta):,.2f} from {params[0]} to {params[1]}" + (f" ({abs(pct):.2f}%)." if pct is not None else ".")
    elif intent == "monthly_trend":
        result = {"months":rows}
        summary = f"Revenue is shown for {len(rows)} months, from {rows[0]['month']} to {rows[-1]['month']}." if rows else "No sales data is available."
    elif intent == "total_revenue":
        result = rows[0]
        summary = f"Total revenue is ₹{result['value']:,.2f} across {result['transactions']} completed transactions ({result['units']} units)."
    elif rows:
        result = rows[0]
        if intent == "top_product": summary = f"{result['label']} generated the highest revenue: ₹{result['revenue']:,.2f} ({result['units']} units)."
        else: summary = f"{result['label']} generated the highest sales: ₹{result['revenue']:,.2f} across {result['transactions']} transactions."
    else:
        result, summary = {}, "No sales data is available."
    return {"question":question, "intent":intent, "label":LABELS[intent], "answer":summary, "supporting_data":result,
            "method":"Database query over completed, valid transactions. Revenue = quantity × unit price − discount."}


HTML = r'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Sales BI Assistant</title><style>
:root{font-family:Inter,ui-sans-serif,system-ui,Segoe UI,sans-serif;color:#182230;background:#f4f7fb}*{box-sizing:border-box}body{margin:0}.top{background:#102b46;color:white;padding:25px max(calc((100vw - 980px)/2),24px)}.top small{color:#9cc4df;text-transform:uppercase;letter-spacing:.12em;font-weight:700}.top h1{margin:8px 0 4px;font-size:27px}.top p{margin:0;color:#c4d2df}.wrap{max-width:980px;margin:32px auto;padding:0 22px}.card{background:#fff;border:1px solid #e1e8ef;border-radius:14px;padding:24px;box-shadow:0 8px 25px #15354f0c}.ask{display:flex;gap:10px;margin-top:10px}input{flex:1;padding:14px;border:1px solid #cbd6e0;border-radius:9px;font:inherit}button{border:0;border-radius:9px;background:#147d77;color:white;padding:0 22px;font-weight:700;font:inherit;cursor:pointer}button:disabled{opacity:.6}.chips{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}.chip{background:#eef5f8;color:#245674;border:1px solid #dce9ef;border-radius:999px;padding:7px 12px;font-size:13px;cursor:pointer}.result{margin-top:20px;display:none}.answer{font-size:20px;font-weight:650;margin:8px 0 18px}.tag{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:#147d77}.table{width:100%;border-collapse:collapse;margin-top:12px}.table th,.table td{text-align:left;padding:10px;border-bottom:1px solid #e8edf2}.table th{color:#5b6875;font-size:12px;text-transform:uppercase}.note{color:#617080;font-size:13px;margin-top:16px}.error{color:#a22;background:#fff2f1;border-radius:8px;padding:12px}.footer{color:#6e7b89;font-size:12px;margin:18px 2px}
</style></head><body><header class="top"><small>Business intelligence · sales</small><h1>Sales BI Assistant</h1><p>Ask a question. Get an answer backed by the transaction database.</p></header><main class="wrap"><section class="card"><strong>What would you like to know?</strong><form id="form" class="ask"><input id="question" autocomplete="off" placeholder="e.g. Which product generated the highest revenue?" maxlength="500" required><button id="submit">Ask</button></form><div class="chips"><button class="chip" type="button">What is the total revenue?</button><button class="chip" type="button">Which product generated the highest revenue?</button><button class="chip" type="button">Show monthly revenue trend</button><button class="chip" type="button">Which region generated the highest sales?</button><button class="chip" type="button">How did revenue change between January 2026 and February 2026?</button></div><div class="result" id="result"></div></section><p class="footer">Revenue uses completed transactions only: quantity × unit price − discount. Answers are limited to supported sales questions.</p></main><script>
const form=document.querySelector('#form'),input=document.querySelector('#question'),out=document.querySelector('#result'),submit=document.querySelector('#submit');document.querySelectorAll('.chip').forEach(b=>b.onclick=()=>{input.value=b.textContent;form.requestSubmit()});
function table(data){let rows=Array.isArray(data)?data:(data.months||[data]);if(!rows.length)return '';let keys=Object.keys(rows[0]);return '<table class="table"><thead><tr>'+keys.map(k=>'<th>'+k.replaceAll('_',' ')+'</th>').join('')+'</tr></thead><tbody>'+rows.map(r=>'<tr>'+keys.map(k=>'<td>'+String(r[k]??'—')+'</td>').join('')+'</tr>').join('')+'</tbody></table>'}
form.onsubmit=async e=>{e.preventDefault();submit.disabled=true;submit.textContent='Working…';out.style.display='block';out.innerHTML='';try{let res=await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:input.value})}),d=await res.json();if(!res.ok)throw Error(d.error||'Request failed');out.innerHTML='<div class="tag">'+d.label+'</div><div class="answer">'+d.answer+'</div>'+table(d.supporting_data)+'<div class="note">'+d.method+'</div>'}catch(err){out.innerHTML='<div class="error">'+err.message+'</div>'}finally{submit.disabled=false;submit.textContent='Ask'}};
</script></body></html>'''


def application(environ, start_response):
    path, method = environ.get("PATH_INFO", "/"), environ.get("REQUEST_METHOD", "GET")
    try:
        if path == "/" and method == "GET":
            body, status, mime = HTML.encode(), HTTPStatus.OK, "text/html; charset=utf-8"
        elif path == "/api/health" and method == "GET":
            conn=connect(); n=conn.execute("SELECT COUNT(*) FROM sales").fetchone()[0]; conn.close()
            body=json.dumps({"status":"ok","transactions":n}).encode(); status=HTTPStatus.OK; mime="application/json"
        elif path == "/api/ask" and method == "POST":
            length=int(environ.get("CONTENT_LENGTH") or 0)
            if length > 10000: raise ValueError("Request body is too large.")
            payload=json.loads(environ["wsgi.input"].read(length) or b"{}")
            question=payload.get("question") if isinstance(payload,dict) else None
            if not isinstance(question,str): raise ValueError("JSON body must include a string 'question'.")
            body=json.dumps(answer(question),ensure_ascii=False).encode(); status=HTTPStatus.OK; mime="application/json; charset=utf-8"
        else:
            body=json.dumps({"error":"Not found"}).encode(); status=HTTPStatus.NOT_FOUND; mime="application/json"
    except (ValueError, json.JSONDecodeError) as exc:
        body=json.dumps({"error":str(exc)}).encode(); status=HTTPStatus.BAD_REQUEST; mime="application/json"
    except Exception:
        body=json.dumps({"error":"The request could not be processed."}).encode(); status=HTTPStatus.INTERNAL_SERVER_ERROR; mime="application/json"
    start_response(f"{status.value} {status.phrase}", [("Content-Type",mime),("Content-Length",str(len(body))),("X-Content-Type-Options","nosniff")])
    return [body]


if __name__ == "__main__":
    from wsgiref.simple_server import make_server
    stats=initialize()
    print(f"Imported {stats['imported_rows']} valid transactions; excluded {stats['excluded_rows']} rows.")
    print("Open http://127.0.0.1:8000")
    make_server("127.0.0.1",8000,application).serve_forever()
