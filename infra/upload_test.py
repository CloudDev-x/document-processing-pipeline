import json
import subprocess
import sys
import urllib.request

api_url, api_key = sys.argv[1], sys.argv[2]

req = urllib.request.Request(
    api_url + "/documents",
    data=b'{"content_type":"text/plain"}',
    headers={"x-api-key": api_key, "Content-Type": "application/json"},
    method="POST",
)
resp = json.load(urllib.request.urlopen(req))
upload = resp["upload"]

cmd = ["curl", "-s", "-o", "NUL", "-w", "upload http %{http_code}\n", upload["url"]]
for name, value in upload["fields"].items():
    cmd += ["-F", f"{name}={value}"]
cmd += ["-F", "file=@statement-api-001.txt;type=text/plain"]
subprocess.run(cmd, check=True)

print("document_id:", resp["document_id"])
