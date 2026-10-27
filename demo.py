#!/usr/bin/env python3
"""Deploy + jalankan demo BPMN "E-Commerce Order Fulfillment" di Camunda 7.

Jalankan: python3 demo.py
Script ini akan menyalakan docker compose (Camunda Run + mock web service),
men-deploy ecommerce-order.bpmn, lalu menguji 3 skenario:
  1. stok tersedia  -> approval -> pembayaran -> pengemasan -> pengiriman
  2. stok habis     -> gateway memilih jalur pembatalan
  3. timer 20 detik -> pengingat approval terkirim (non-interrupting)
"""
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BPMN = ROOT / "ecommerce-order.bpmn"
JAR = ROOT / "userlib" / "camunda-connect-connectors-all-7.24.0.jar"
JAR_URL = ("https://repo1.maven.org/maven2/org/camunda/connect/"
           "camunda-connect-connectors-all/7.24.0/"
           "camunda-connect-connectors-all-7.24.0.jar")
ENGINE = "http://localhost:8080/engine-rest"
MOCK = "http://localhost:8081"


def parse(raw):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw.decode(errors="replace")


def http(method, path, body=None, base=ENGINE, headers=None, timeout=30):
    data = None
    hdrs = dict(headers or {})
    if isinstance(body, (dict, list)):
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    elif isinstance(body, bytes):
        data = body
    req = urllib.request.Request(base + path, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, parse(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, parse(exc.read())
    except OSError as exc:
        raise RuntimeError(f"Tidak bisa menghubungi {base}{path}: {exc}") from exc


def engine_up():
    try:
        return http("GET", "/version", timeout=3)[0] == 200
    except RuntimeError:
        return False


def wait_engine(timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if engine_up():
            return
        time.sleep(2)
    raise RuntimeError("Camunda tidak siap setelah 3 menit. Cek: docker compose logs camunda")


def ensure_stack():
    if engine_up():
        print("[i] Camunda sudah berjalan.")
        return
    if not JAR.exists():
        print("[i] Mengunduh HTTP connector Camunda...")
        JAR.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(JAR_URL, JAR)
    print("[i] Menjalankan Camunda Run + mock API (docker compose)...")
    subprocess.run(["docker", "compose", "up", "-d"], cwd=ROOT, check=True)
    wait_engine()


def deploy():
    boundary = "----komlay" + uuid.uuid4().hex
    chunks = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="deployment-name"\r\n\r\n'
        f'komlay-{int(time.time())}\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="deployment-source"\r\n\r\n'
        f'demo.py\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="ecommerce-order.bpmn"; '
        f'filename="ecommerce-order.bpmn"\r\nContent-Type: application/xml\r\n\r\n'.encode(),
        BPMN.read_bytes(), b"\r\n",
        f'--{boundary}--\r\n'.encode(),
    ]
    status, result = http(
        "POST", "/deployment/create", b"".join(chunks),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert status == 200, f"Deploy gagal ({status}): {result}"
    defs = (result.get("deployedProcessDefinitions") or {})
    assert defs, f"BPMN tidak berisi process definition: {result}"
    name, definition = next(iter(defs.items()))
    print(f"[✓] Deploy berhasil: {name} (v{definition['version']})")
    return definition["id"]


def start_order(order_id, stock):
    variables = {
        "orderId": {"value": order_id, "type": "String"},
        "sku": {"value": "SKU-001", "type": "String"},
        "qty": {"value": 2, "type": "Integer"},
        "amount": {"value": 150000, "type": "Double"},
        "stockAvailable": {"value": stock, "type": "Boolean"},
    }
    status, result = http("POST", "/message", {
        "messageName": "orderPlaced",
        "businessKey": order_id,
        "processVariables": variables,
    })
    assert status == 204, f"Start message gagal ({status}): {result}"
    proc = wait_instance(order_id)
    print(f"[✓] Instance {order_id} dibuat (pid {proc['id'][:8]}..., stok={stock})")
    return proc


def wait_instance(order_id, timeout=15):
    deadline = time.time() + timeout
    key = urllib.parse.quote(order_id, safe="")
    while time.time() < deadline:
        status, data = http("GET", f"/history/process-instance?processInstanceBusinessKey={key}")
        assert status == 200, f"Query history gagal ({status}): {data}"
        if data:
            return data[0]
        time.sleep(0.3)
    raise RuntimeError(f"Instance {order_id} tidak ditemukan")


def wait_task(pid, task_key, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, tasks = http("GET", f"/task?processInstanceId={pid}")
        assert status == 200, f"Query task gagal ({status}): {tasks}"
        for task in tasks:
            if task["taskDefinitionKey"] == task_key:
                return task
        time.sleep(0.3)
    raise RuntimeError(f"Task {task_key} tidak muncul untuk instance {pid}")


def complete(task, variables=None):
    status, result = http("POST", f"/task/{task['id']}/complete",
                          {"variables": variables or {}})
    assert status == 204, f"Complete task gagal ({status}): {result}"
    print(f"[✓] Human task selesai: {task['name']} ({task['taskDefinitionKey']})")


def wait_ended(pid, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _ = http("GET", f"/process-instance/{pid}")
        if status == 404:
            return
        time.sleep(0.3)
    raise RuntimeError(f"Instance {pid} belum selesai")


def end_events(pid):
    status, activities = http("GET", f"/history/activity-instance?processInstanceId={pid}")
    assert status == 200, f"Query history aktivitas gagal ({status}): {activities}"
    ends = {"EndEvent_Completed", "EndEvent_Cancelled", "EndEvent_ReminderSent"}
    return [item.get("activityId") for item in activities if item.get("activityId") in ends]


def calls_for(order_id):
    status, data = http("GET", "/calls", base=MOCK)
    assert status == 200, f"Mock API tidak merespons ({status}): {data}"
    return [c for c in data["calls"] if (c.get("payload") or {}).get("orderId") == order_id]


def expect_calls(order_id, expected):
    paths = {call["path"] for call in calls_for(order_id)}
    missing = set(expected) - paths
    assert not missing, f"Web service tidak terpanggil: {missing} (dipanggil: {paths})"
    print(f"[✓] Web service terpanggil: {', '.join(sorted(expected))}")


def main():
    if not BPMN.exists():
        sys.exit(f"File BPMN tidak ditemukan: {BPMN}")
    ensure_stack()
    deploy()
    suffix = int(time.time())

    print("\n=== Skenario 1: stok tersedia, pesanan diproses penuh ===")
    order_id = f"ORD-{suffix}-A"
    proc = start_order(order_id, stock=True)
    approval = wait_task(proc["id"], "UserTask_ManagerApproval")
    print(f"[..] Menunggu human task: {approval['name']} (assignee: {approval['assignee']})")
    complete(approval, {"approved": {"value": True, "type": "Boolean"}})
    packing = wait_task(proc["id"], "UserTask_WarehousePacking")
    print(f"[..] Menunggu human task: {packing['name']} (assignee: {packing['assignee']})")
    complete(packing)
    wait_ended(proc["id"])
    ends = end_events(proc["id"])
    assert "EndEvent_Completed" in ends, f"End event salah: {ends}"
    expect_calls(order_id, ["/inventory/reserve", "/payment/charge", "/shipment/book"])
    print(f"[✓] Pesanan {order_id} selesai (EndEvent_Completed)")

    print("\n=== Skenario 2: stok habis, gateway membatalkan pesanan ===")
    order_id = f"ORD-{suffix}-B"
    proc = start_order(order_id, stock=False)
    wait_ended(proc["id"])
    ends = end_events(proc["id"])
    assert "EndEvent_Cancelled" in ends, f"End event salah: {ends}"
    expect_calls(order_id, ["/inventory/reserve", "/inventory/release"])
    print(f"[✓] Pesanan {order_id} dibatalkan (EndEvent_Cancelled)")

    print("\n=== Skenario 3: timer 20 detik mengirim pengingat approval ===")
    order_id = f"ORD-{suffix}-C"
    proc = start_order(order_id, stock=True)
    approval = wait_task(proc["id"], "UserTask_ManagerApproval")
    print("[..] Menunggu timer boundary event (job executor Camunda bisa delay hingga ~60 detik)...")
    deadline = time.time() + 90
    while time.time() < deadline:
        if any(call["path"] == "/notification/reminder" for call in calls_for(order_id)):
            break
        time.sleep(2)
    expect_calls(order_id, ["/notification/reminder"])
    ends = end_events(proc["id"])
    assert "EndEvent_ReminderSent" in ends, f"Jalur timer tidak selesai: {ends}"
    assert http("GET", f"/task/{approval['id']}")[0] == 200, "Timer seharusnya non-interrupting"
    print("[✓] Pengingat terkirim, task approval tetap aktif (non-interrupting)")
    complete(approval, {"approved": {"value": True, "type": "Boolean"}})
    packing = wait_task(proc["id"], "UserTask_WarehousePacking")
    complete(packing)
    wait_ended(proc["id"])
    ends = end_events(proc["id"])
    assert "EndEvent_Completed" in ends, f"Proses utama tidak selesai: {ends}"
    expect_calls(order_id, ["/inventory/reserve", "/payment/charge", "/shipment/book", "/notification/reminder"])
    print(f"[✓] Pesanan {order_id} selesai setelah approval")

    print("\n=== SEMUA SKENARIO LULUS ===")
    print("Cockpit : http://localhost:8080/camunda/app/cockpit  (login: demo / demo)")
    print("Tasklist: http://localhost:8080/camunda/app/tasklist  (login: demo / demo)")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        sys.exit(f"\n[✗] GAGAL: {exc}")
    except RuntimeError as exc:
        sys.exit(f"\n[✗] GAGAL: {exc}")
