import re
import requests

URL = "https://services.swpc.noaa.gov/text/daily-solar-indices.txt"
txt = requests.get(URL, timeout=20, headers={"Cache-Control": "no-cache"}).text

# Each daily record begins with: YYYY MM DD <10.7cm_flux> <sunspot_number> ...
rows = re.findall(r"(\d{4})\s+(\d{2})\s+(\d{2})\s+(\d+)\s+(\d+)", txt)
y, m, d, f107, ssn = rows[-1]

print(f"Latest SWPC daily record: {y}-{m}-{d}  SSN={ssn}  F10.7={f107}")
