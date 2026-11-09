#!/bin/bash
# Çift tıklayınca sunucuyu başlatır ve arayüzü tarayıcıda açar. Kapatmak için bu pencerede Ctrl+C.
cd "$(dirname "$0")" || exit 1
(sleep 2; open "http://localhost:8765") &
exec python3 -u server.py
