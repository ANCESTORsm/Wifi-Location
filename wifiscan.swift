// Çevredeki WiFi sinyallerini okur, stdout'a satır satır JSON yazar. İki tür satır üretir:
//   {"type":"scan", ...}  tüm ağların sinyal gücü. macOS taramayı kısıtladığı için ~13 saniyede bir gelir.
//   {"type":"cur",  ...}  bağlı olunan ağın sinyali: yarım saniyede bir, saniyede ~10 ham örnekle birlikte.
// stdin'den "scan off" / "scan on" satırlarıyla tam tarama duraklatılır (hareket algılamada kesintisiz örnek için).
// Çalıştırma: swift wifiscan.swift   (derlenmiş hali ağ adlarını okuyamıyor, o yüzden yorumlayıcıyla çalışır)
import CoreWLAN
import Foundation

func emit(_ obj: [String: Any]) {
    if let d = try? JSONSerialization.data(withJSONObject: obj),
       let s = String(data: d, encoding: .utf8) {
        print(s)
        fflush(stdout)
    }
}

final class Settings {
    private let lock = NSLock()
    private var scan = true
    var scanEnabled: Bool {
        get { lock.lock(); defer { lock.unlock() }; return scan }
        set { lock.lock(); scan = newValue; lock.unlock() }
    }
}
let settings = Settings()

Thread.detachNewThread {
    while let line = readLine() {
        switch line.trimmingCharacters(in: .whitespaces) {
        case "scan off": settings.scanEnabled = false
        case "scan on": settings.scanEnabled = true
        default: break
        }
    }
}

let client = CWWiFiClient.shared()

while true {
    autoreleasepool {
        guard let iface = client.interface(), iface.powerOn() else {
            emit(["type": "error", "error": "wifi_off"])
            sleep(2)
            return
        }
        if settings.scanEnabled {
            do {
                let t0 = Date()
                let nets = try iface.scanForNetworks(withSSID: nil)
                let t1 = Date()
                // Hemen dönen cevap önbellekten gelir (eski veri); yalnızca gerçek taramaları bildir.
                if t1.timeIntervalSince(t0) > 0.3 {
                    var arr: [[String: Any]] = []
                    for n in nets {
                        arr.append([
                            "ssid": n.ssid ?? "",
                            "bssid": n.bssid ?? "",
                            "ch": n.wlanChannel?.channelNumber ?? 0,
                            "rssi": n.rssiValue,
                        ])
                    }
                    emit([
                        "type": "scan",
                        "t0": t0.timeIntervalSince1970,
                        "t": t1.timeIntervalSince1970,
                        "nets": arr,
                    ])
                }
            } catch {
                emit(["type": "error", "error": "scan_failed", "detail": error.localizedDescription])
            }
        }
        var samples: [Int] = []
        let start = Date()
        for _ in 0..<5 {
            let v = iface.rssiValue()
            if v < 0 { samples.append(v) }
            usleep(85_000)
        }
        if !samples.isEmpty {
            let end = Date()
            emit([
                "type": "cur",
                "t0": start.timeIntervalSince1970,
                "t": end.timeIntervalSince1970,
                "rssi": Double(samples.reduce(0, +)) / Double(samples.count),
                "samples": samples,
                "ch": iface.wlanChannel()?.channelNumber ?? 0,
            ])
        }
    }
}
