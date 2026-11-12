#!/usr/bin/env python3
"""Deploy + jalankan demo BPMN "E-Commerce Order Fulfillment" di Camunda 7.

Jalankan pengujian: python3 demo.py
Jalankan lewat Tasklist: python3 demo.py --interactive
Script ini akan menyalakan docker compose (Camunda Run + mock web service),
men-deploy ecommerce-order.bpmn, lalu menguji 6 skenario:
  1. disetujui -> pembayaran -> invoice || kemas + konfirmasi + QC -> booking
  2. ditolak manajer -> pelepasan reservasi -> dibatalkan
  3. stok habis -> pelepasan reservasi -> dibatalkan
  4. timer 20 detik -> pengingat (non-interrupting) -> persetujuan -> booking
  5. inspeksi gagal sekali -> kemas ulang -> inspeksi lolos -> booking
  6. inspeksi gagal dua kali -> dua putaran rework -> inspeksi lolos -> booking
"""
import argparse
import json
import os
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
MOCK = f"http://localhost:{os.environ.get('MOCK_API_PORT', '18081')}"


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


def mock_up():
    try:
        status, data = http("GET", "/health", base=MOCK, timeout=3)
        return status == 200 and isinstance(data, dict) and data.get("service") == "mock-api"
    except RuntimeError:
        return False


def wait_engine(timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if engine_up():
            return
        time.sleep(2)
    raise RuntimeError("Camunda tidak siap setelah 3 menit. Cek: docker compose logs camunda")


def wait_mock(timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if mock_up():
            return
        time.sleep(1)
    raise RuntimeError(
        f"Mock API tidak siap di {MOCK}. Cek: docker compose logs mock-api. "
        "Jika port dipakai aplikasi lain, atur MOCK_API_PORT ke port kosong lalu jalankan ulang."
    )


def ensure_stack():
    if engine_up() and mock_up():
        print("[i] Camunda dan mock API sudah berjalan.")
        return
    if not JAR.exists():
        print("[i] Mengunduh HTTP connector Camunda...")
        JAR.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(JAR_URL, JAR)
    print("[i] Menjalankan Camunda Run + mock API (docker compose)...")
    subprocess.run(["docker", "compose", "up", "-d"], cwd=ROOT, check=True)
    wait_engine()
    wait_mock()


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


def start_order(order_id, stock, manager_user=None, warehouse_user=None, inspector_user=None):
    variables = {
        "orderId": {"value": order_id, "type": "String"},
        "sku": {"value": "SKU-001", "type": "String"},
        "qty": {"value": 2, "type": "Integer"},
        "amount": {"value": 150000, "type": "Double"},
        "stockAvailable": {"value": stock, "type": "Boolean"},
        "approved": {"value": False, "type": "Boolean"},
        "qualityPassed": {"value": False, "type": "Boolean"},
    }
    for name, value in (("managerUser", manager_user), ("warehouseUser", warehouse_user),
                        ("inspectorUser", inspector_user)):
        if value is not None:
            variables[name] = {"value": value, "type": "String"}
    status, result = http("POST", "/process-definition/key/Process_EcommerceOrder/start", {
        "businessKey": order_id,
        "variables": variables,
    })
    assert status == 200, f"Start process gagal ({status}): {result}"
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
    print(f"[✓] User task selesai: {task['name']} ({task['taskDefinitionKey']})")


def wait_ended(pid, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _ = http("GET", f"/process-instance/{pid}")
        if status == 404:
            return
        time.sleep(0.3)
    raise RuntimeError(f"Instance {pid} belum selesai")


def activity_history(pid):
    status, activities = http("GET", f"/history/activity-instance?processInstanceId={pid}")
    assert status == 200, f"Query history aktivitas gagal ({status}): {activities}"
    return activities


def end_events(pid):
    ends = {"EndEvent_Completed", "EndEvent_Cancelled", "EndEvent_ReminderSent"}
    return [item.get("activityId") for item in activity_history(pid) if item.get("activityId") in ends]


def calls_for(order_id):
    status, data = http("GET", "/calls", base=MOCK)
    assert status == 200, f"Mock API tidak merespons ({status}): {data}"
    return [c for c in data["calls"] if (c.get("payload") or {}).get("orderId") == order_id]


def expect_calls(order_id, expected, forbidden=()):
    paths = {call["path"] for call in calls_for(order_id)}
    missing = set(expected) - paths
    assert not missing, f"Web service tidak terpanggil: {missing} (dipanggil: {paths})"
    unexpected = set(forbidden) & paths
    assert not unexpected, f"Web service seharusnya tidak terpanggil: {unexpected}"
    print(f"[✓] Web service terpanggil: {', '.join(sorted(expected))}")


def expect_quality_wait(pid, order_id):
    expect_calls(order_id, ["/inventory/reserve", "/payment/charge", "/invoice/create"],
                 forbidden=["/shipment/book", "/inventory/release"])
    paths = [call["path"] for call in calls_for(order_id)]
    for path in ("/inventory/reserve", "/payment/charge", "/invoice/create"):
        assert paths.count(path) == 1, f"Rework tidak boleh mengulang {path}: {paths}"
    activities = activity_history(pid)
    assert len([a for a in activities if a["activityId"] == "ServiceTask_CreateInvoice" and a["endTime"]]) == 1
    assert any(a["activityId"] == "Gateway_OrderReady" and not a["endTime"] for a in activities), \
        "Parallel join harus menunggu hasil QC yang lolos"
    return activities


def finish_packing(pid, order_id, inspection_results=(True,)):
    assert inspection_results and inspection_results[-1] is True, "Skenario harus berakhir dengan QC lolos"
    assert all(result is False for result in inspection_results[:-1]), "Hasil sebelum akhir harus gagal"
    confirmations, inspections = set(), set()
    for attempt, quality_passed in enumerate(inspection_results, 1):
        packing = wait_task(pid, "UserTask_WarehouseConfirmation")
        assert packing["assignee"] == "demo", packing
        assert packing["id"] not in confirmations, "Setiap rework harus membuat task konfirmasi baru"
        confirmations.add(packing["id"])
        activities = expect_quality_wait(pid, order_id)
        assert len([a for a in activities if a["activityId"] == "ManualTask_WarehousePacking"
                    and a["activityType"] == "manualTask" and a["endTime"]]) == attempt
        if attempt == 1:
            status, variable = http("GET", f"/process-instance/{pid}/variables/invoiceResponse")
            assert status == 200, f"Respons invoice tidak tersimpan ({status}): {variable}"
            invoice = json.loads(variable["value"])
            assert invoice["orderId"] == order_id and invoice["invoiceId"] == f"INV-{order_id}", invoice
        else:
            # Sengaja simulasikan nilai lama true untuk menguji reset saat QC berikutnya dimulai.
            status, result = http("PUT", f"/process-instance/{pid}/variables/qualityPassed",
                                  {"value": True, "type": "Boolean"})
            assert status == 204, result
        print(f"[i] Putaran {attempt}: demo mensimulasikan pengemasan fisik telah selesai.")
        complete(packing)
        inspection = wait_task(pid, "UserTask_QualityInspection")
        assert inspection["assignee"] == "demo", inspection
        assert inspection["id"] not in inspections, "Setiap putaran harus membuat task QC baru"
        inspections.add(inspection["id"])
        status, variables = http("GET", f"/task/{inspection['id']}/form-variables?variableNames=qualityPassed")
        assert status == 200, variables
        assert variables["qualityPassed"]["type"] == "Boolean" and variables["qualityPassed"]["value"] is False, \
            "Form inspeksi baru harus mulai dari belum lolos, termasuk setelah nilai lama true"
        activities = expect_quality_wait(pid, order_id)
        assert len([a for a in activities if a["activityId"] == "ManualTask_InspectPackaging"
                    and a["activityType"] == "manualTask" and a["endTime"]]) == attempt
        print(f"[i] Putaran {attempt}: demo mensimulasikan inspeksi fisik; hasil={'lolos' if quality_passed else 'perlu perbaikan'}.")
        complete(inspection, {"qualityPassed": {"value": quality_passed, "type": "Boolean"}})
        if not quality_passed:
            rework = wait_task(pid, "UserTask_WarehouseConfirmation")
            assert rework["id"] not in confirmations, rework
            expect_quality_wait(pid, order_id)
            print("[✓] QC gagal: kembali ke gudang, booking tetap tertahan.")
    wait_ended(pid)
    ends = end_events(pid)
    assert "EndEvent_Completed" in ends and "EndEvent_Cancelled" not in ends, f"End event salah: {ends}"
    expect_calls(order_id, ["/inventory/reserve", "/payment/charge", "/invoice/create", "/shipment/book"],
                 forbidden=["/inventory/release"])
    paths = [call["path"] for call in calls_for(order_id)]
    for path in ("/inventory/reserve", "/payment/charge", "/invoice/create", "/shipment/book"):
        assert paths.count(path) == 1, f"API harus dipanggil tepat sekali: {path}, {paths}"
    activities = activity_history(pid)
    for activity_id in ("ManualTask_WarehousePacking", "UserTask_WarehouseConfirmation",
                        "ManualTask_InspectPackaging", "UserTask_QualityInspection"):
        assert len([a for a in activities if a["activityId"] == activity_id and a["endTime"]]) == len(inspection_results), \
            f"Jumlah putaran tidak sesuai untuk {activity_id}"
    print(f"[✓] Pengiriman {order_id} terjadwal (EndEvent_Completed)")


def verify_cancelled(pid, order_id, reason):
    wait_ended(pid)
    ends = end_events(pid)
    assert "EndEvent_Cancelled" in ends and "EndEvent_Completed" not in ends, f"End event salah: {ends}"
    expect_calls(order_id, ["/inventory/reserve", "/inventory/release"],
                 forbidden=["/payment/charge", "/invoice/create", "/shipment/book"])
    releases = [c for c in calls_for(order_id) if c["path"] == "/inventory/release"]
    assert len(releases) == 1 and releases[0]["payload"]["reason"] == reason, releases
    status, tasks = http("GET", f"/task?processInstanceId={pid}")
    assert status == 200 and not tasks, f"Pesanan dibatalkan tidak boleh menyisakan user task: {tasks}"
    print(f"[✓] Pesanan {order_id} dibatalkan ({reason})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interactive", action="store_true",
                        help="Mulai satu pesanan dan biarkan user task diselesaikan lewat Tasklist.")
    args = parser.parse_args()
    if not BPMN.exists():
        sys.exit(f"File BPMN tidak ditemukan: {BPMN}")
    ensure_stack()
    deploy()
    suffix = f"{int(time.time())}-{uuid.uuid4().hex[:6]}"

    if args.interactive:
        order_id = f"ORD-{suffix}-MANUAL"
        proc = start_order(order_id, stock=True)
        approval = wait_task(proc["id"], "UserTask_ManagerApproval")
        assert approval["assignee"] == "demo", approval
        print(f"[✓] Instance tetap aktif: {order_id} (pid {proc['id']})")
        print("Login demo / demo. Buka task Persetujuan manajer pada tab Form.")
        print("Centang Setujui pesanan untuk menyetujui; tidak dicentang berarti menolak. Klik Complete.")
        print("Jika disetujui, kemas barang secara fisik lalu selesaikan Konfirmasi pengemasan selesai.")
        print("Petugas QC memeriksa kemasan, lalu mencatat hasil pada task Catat hasil inspeksi.")
        print("Centang Inspeksi lulus untuk melanjutkan; tidak dicentang mengembalikan pekerjaan ke gudang.")
        print("Ulangi pengemasan, konfirmasi, dan inspeksi sampai lolos. Semua peran demo memakai akun demo.")
        print(f"Tasklist: http://localhost:8080/camunda/app/tasklist/default/#/?task={approval['id']}")
        print(f"Cockpit : http://localhost:8080/camunda/app/cockpit/default/#/process-instance/{proc['id']}")
        return

    print("\n=== Skenario 1: disetujui, invoice dan pengemasan paralel ===")
    order_id = f"ORD-{suffix}-A"
    proc = start_order(order_id, stock=True)
    approval = wait_task(proc["id"], "UserTask_ManagerApproval")
    assert approval["assignee"] == "demo", approval
    complete(approval, {"approved": {"value": True, "type": "Boolean"}})
    finish_packing(proc["id"], order_id)

    print("\n=== Skenario 2: manajer menolak pesanan ===")
    order_id = f"ORD-{suffix}-B"
    proc = start_order(order_id, stock=True)
    approval = wait_task(proc["id"], "UserTask_ManagerApproval")
    complete(approval, {"approved": {"value": False, "type": "Boolean"}})
    verify_cancelled(proc["id"], order_id, "ditolak-manajer")

    print("\n=== Skenario 3: stok habis, gateway membatalkan pesanan ===")
    order_id = f"ORD-{suffix}-C"
    proc = start_order(order_id, stock=False)
    verify_cancelled(proc["id"], order_id, "stok-habis")

    print("\n=== Skenario 4: timer 20 detik mengirim pengingat approval ===")
    order_id = f"ORD-{suffix}-D"
    proc = start_order(order_id, stock=True)
    approval = wait_task(proc["id"], "UserTask_ManagerApproval")
    print("[..] Menunggu timer boundary event (job executor Camunda bisa delay hingga ~60 detik)...")
    deadline = time.time() + 90
    while time.time() < deadline:
        if (any(call["path"] == "/notification/reminder" for call in calls_for(order_id))
                and "EndEvent_ReminderSent" in end_events(proc["id"])):
            break
        time.sleep(2)
    else:
        raise RuntimeError("Jalur pengingat tidak selesai dalam 90 detik")
    expect_calls(order_id, ["/notification/reminder"])
    ends = end_events(proc["id"])
    assert "EndEvent_ReminderSent" in ends, f"Jalur timer tidak selesai: {ends}"
    assert http("GET", f"/task/{approval['id']}")[0] == 200, "Timer seharusnya non-interrupting"
    print("[✓] Pengingat terkirim, task approval tetap aktif (non-interrupting)")
    complete(approval, {"approved": {"value": True, "type": "Boolean"}})
    finish_packing(proc["id"], order_id)

    for number, results in ((5, (False, True)), (6, (False, False, True))):
        failures = len(results) - 1
        print(f"\n=== Skenario {number}: QC gagal {failures} kali, lalu rework hingga lolos ===")
        order_id = f"ORD-{suffix}-REWORK-{failures}"
        proc = start_order(order_id, stock=True)
        approval = wait_task(proc["id"], "UserTask_ManagerApproval")
        complete(approval, {"approved": {"value": True, "type": "Boolean"}})
        finish_packing(proc["id"], order_id, inspection_results=results)

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
