#!/usr/bin/env bash
# VPS'in çıkış IP'sini ve borsalara erişimi test eder (HTTP 451/403 = restricted location).
# Kullanım:  bash check_exchange_access.sh
set -uo pipefail

echo "Çıkış IP (ipify):   $(curl -s --max-time 10 https://api.ipify.org || echo HATA)"
echo "Çıkış IP (ifconfig): $(curl -s --max-time 10 https://ifconfig.me/ip || echo HATA)"
echo

FAIL=0
for url in \
  https://api.binance.com/api/v3/ping \
  https://fapi.binance.com/fapi/v1/ping \
  https://testnet.binance.vision/api/v3/ping \
  https://demo-fapi.binance.com/fapi/v1/ping \
  https://api.btcturk.com/api/v2/server/exchangeinfo
do
  code=$(curl -s -o /dev/null --max-time 15 -w '%{http_code}' "$url")
  case "$code" in
    200) echo "TAMAM      $code  $url" ;;
    451|403) echo "ENGELLİ    $code  $url  -> restricted location: VPS bölgesini değiştirin"; FAIL=1 ;;
    000) echo "ULAŞILAMADI     $url"; FAIL=1 ;;
    *) echo "BEKLENMEDİK $code $url"; FAIL=1 ;;
  esac
done
exit $FAIL
