import hmac
import hashlib
import time
import requests
import json
import traceback
import yaml
import logging
import math

# Logging Setup
logger = logging.getLogger('OrderManager')
logger.setLevel(logging.INFO)  # INFO-Level für reduzierte Logs (wichtige Events und Fehler)

# NOTE: API-Keys werden jetzt über den Constructor übergeben
# Single-Account: Beide Bots verwenden die gleichen API-Keys


class BybitOrderManager:
    def __init__(self, api_key, secret_key):
        self.api_key = api_key
        self.secret_key = secret_key
        # Erhöhtes recv_window für VPN-Latenz (von 5000ms auf 20000ms)
        self.recv_window = str(20000)
        self.base_url = "https://api.bybit.com"
        # Cache für Preise (30 Sekunden) um Rate-Limits zu vermeiden
        self._price_cache = {}
        self._price_cache_timeout = 30  # seconds
        # WICHTIG: Session für bessere Performance (spart TCP/TLS-Handshake)
        self._session = requests.Session()
        self._symbol_tick_cache = {}

        # Cache pro Symbol, ob Hedge-Mode (Both Sides, mode=3) bereits gesetzt wurde.
        # Key: (category, symbol_upper) -> bool
        self._hedge_mode_cache = {}

        # Cache pro Symbol, ob der gewünschte Leverage bereits gesetzt wurde.
        # Key: (category, symbol_upper) -> str (gesetzter Leverage-Wert) oder True
        self._leverage_cache = {}

    # ----------------------------------------------------------------------
    # Position Mode / Hedge Mode Helpers
    # ----------------------------------------------------------------------

    def ensure_hedge_mode(self, symbol: str, category: str = "linear") -> bool:
        """
        Stellt sicher, dass für das gegebene Symbol der Positionsmodus auf Hedge (Both Sides, mode=3) steht.
        - Ruft /v5/position/switch-mode nur einmal pro (category, symbol) und Manager-Instanz auf.
        - Gibt True zurück, wenn der Modus bereits Hedge war oder erfolgreich gesetzt wurde.
        - Gibt False zurück, wenn der Request fehlschlägt (in dem Fall sollte der Caller die Order nicht senden).
        """
        try:
            sym = (symbol or "").strip().upper()
            cat = (category or "linear").strip()
            cache_key = (cat, sym)
            if self._hedge_mode_cache.get(cache_key):
                return True

            url = f"{self.base_url}/v5/position/switch-mode"
            recv_window = self.recv_window
            body = {
                "category": cat,
                "symbol": sym,
                "mode": 3,  # 3 = Both Sides (Hedge Mode)
            }
            body_json = json.dumps(body)
            timestamp = str(int(time.time() * 1000))
            to_sign = timestamp + self.api_key + recv_window + body_json
            signature = hmac.new(
                self.secret_key.encode("utf-8"),
                to_sign.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            headers = {
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-SIGN": signature,
                "X-BAPI-SIGN-TYPE": "2",
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": recv_window,
                "Content-Type": "application/json",
            }

            logger.info(f"[HEDGE-MODE] Stelle Positionsmodus auf Both Sides für {sym} ({cat})")
            resp = self._session.post(url, headers=headers, data=body_json, timeout=10)
            if resp.status_code != 200:
                logger.error(f"[HEDGE-MODE] HTTP-Fehler {resp.status_code}: {resp.text[:500]}")
                return False
            data = resp.json()
            ret_code = data.get("retCode")
            ret_msg = str(data.get("retMsg") or "")

            # 0 = OK, 110025 = "Position mode is not modified" → Modus war bereits korrekt.
            if ret_code == 0 or ret_code == 110025:
                if ret_code == 0:
                    logger.info(f"[HEDGE-MODE] ✅ Positionsmodus erfolgreich auf Both Sides gesetzt für {sym}")
                else:
                    logger.info(f"[HEDGE-MODE] ✅ Positionsmodus war bereits Both Sides für {sym} (retCode=110025)")
                self._hedge_mode_cache[cache_key] = True
                return True

            logger.error(
                f"[HEDGE-MODE] ❌ switch-mode fehlgeschlagen für {sym}: "
                f"retCode={ret_code} retMsg={ret_msg}"
            )
            return False
        except Exception as exc:
            logger.error(f"[HEDGE-MODE] ❌ Unerwarteter Fehler bei switch-mode für {symbol}: {exc}", exc_info=True)
            return False

    # ----------------------------------------------------------------------
    # Leverage Helpers
    # ----------------------------------------------------------------------

    def _fetch_max_leverage(self, symbol: str, category: str = "linear") -> str | None:
        """
        Fragt über /v5/market/instruments-info den maximalen Leverage für ein Symbol ab.
        Gibt den maxLeverage-Wert als String zurück oder None bei Fehler.
        """
        try:
            sym = (symbol or "").strip().upper()
            cat = (category or "linear").strip()
            params = {
                "category": cat,
                "symbol": sym,
            }
            url = f"{self.base_url}/v5/market/instruments-info"
            logger.info(f"[LEVERAGE] Hole Instruments-Info für {sym} ({cat})")
            resp = self._session.get(url, params=params, timeout=10)
            if resp.status_code != 200:
                logger.error(f"[LEVERAGE] HTTP-Fehler {resp.status_code} bei instruments-info: {resp.text[:500]}")
                return None
            data = resp.json()
            if data.get("retCode") != 0:
                logger.error(
                    f"[LEVERAGE] ❌ instruments-info fehlgeschlagen für {sym}: "
                    f"retCode={data.get('retCode')} retMsg={data.get('retMsg')}"
                )
                return None
            result = data.get("result") or {}
            lst = result.get("list") or []
            if not lst:
                logger.error(f"[LEVERAGE] ❌ instruments-info: keine Daten für {sym} erhalten")
                return None
            item = lst[0]
            lev_filter = item.get("leverageFilter") or {}
            max_lev = lev_filter.get("maxLeverage")
            if not max_lev:
                logger.error(f"[LEVERAGE] ❌ instruments-info: maxLeverage fehlt für {sym}")
                return None
            logger.info(f"[LEVERAGE] maxLeverage für {sym}: {max_lev}")
            return str(max_lev)
        except Exception as exc:
            logger.error(f"[LEVERAGE] ❌ Unerwarteter Fehler bei instruments-info für {symbol}: {exc}", exc_info=True)
            return None

    def ensure_max_leverage(self, symbol: str, category: str = "linear") -> bool:
        """
        Stellt sicher, dass für das Symbol der Leverage auf den maximal erlaubten Wert gesetzt ist.
        - Fragt einmalig maxLeverage ab.
        - Ruft /v5/position/set-leverage mit buyLeverage=maxLeverage und sellLeverage=maxLeverage auf.
        - Cached den gesetzten Wert pro (category, symbol).
        """
        try:
            sym = (symbol or "").strip().upper()
            cat = (category or "linear").strip()
            cache_key = (cat, sym)
            if self._leverage_cache.get(cache_key):
                return True

            max_lev = self._fetch_max_leverage(sym, cat)
            if not max_lev:
                return False

            url = f"{self.base_url}/v5/position/set-leverage"
            recv_window = self.recv_window
            body = {
                "category": cat,
                "symbol": sym,
                "buyLeverage": str(max_lev),
                "sellLeverage": str(max_lev),
            }
            body_json = json.dumps(body)
            timestamp = str(int(time.time() * 1000))
            to_sign = timestamp + self.api_key + recv_window + body_json
            signature = hmac.new(
                self.secret_key.encode("utf-8"),
                to_sign.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            headers = {
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-SIGN": signature,
                "X-BAPI-SIGN-TYPE": "2",
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": recv_window,
                "Content-Type": "application/json",
            }

            logger.info(f"[LEVERAGE] Setze Leverage auf max={max_lev} für {sym} ({cat})")
            resp = self._session.post(url, headers=headers, data=body_json, timeout=10)
            if resp.status_code != 200:
                logger.error(f"[LEVERAGE] HTTP-Fehler {resp.status_code} bei set-leverage: {resp.text[:500]}")
                return False
            data = resp.json()
            ret_code = data.get("retCode")
            ret_msg = str(data.get("retMsg") or "")

            # 0 = OK, einige Fehlercodes liefern sinngemäß "Leverage is not modified".
            if ret_code == 0 or "not modified" in ret_msg.lower():
                if ret_code == 0:
                    logger.info(f"[LEVERAGE] ✅ Leverage erfolgreich auf {max_lev} gesetzt für {sym}")
                else:
                    logger.info(f"[LEVERAGE] ✅ Leverage war bereits auf {max_lev} gesetzt für {sym} ({ret_msg})")
                self._leverage_cache[cache_key] = str(max_lev)
                return True

            logger.error(
                f"[LEVERAGE] ❌ set-leverage fehlgeschlagen für {sym}: "
                f"retCode={ret_code} retMsg={ret_msg}"
            )
            return False
        except Exception as exc:
            logger.error(f"[LEVERAGE] ❌ Unerwarteter Fehler bei set-leverage für {symbol}: {exc}", exc_info=True)
            return False

    def set_tp_long_order(self, symbol, tp_price, position_size, position_idx=1, trigger_by: str = "MarkPrice"):
        """
        Setzt eine Take-Profit-Order für eine Long-Position.
        
        :param symbol: Handelssymbol (z. B. "ETHUSDT")
        :param tp_price: Take-Profit-Preis
        :param position_size: Positionsgröße
        :param position_idx: Positionsindex (Standard: 1 für Long)
        :return: True wenn erfolgreich, False sonst
        """
        # DETAILLIERTES LOGGING: Funktion wurde aufgerufen
        logger.info("=" * 80)
        logger.info("[SET-TP-LONG] 🚀 START: set_tp_long_order() aufgerufen")
        logger.info(f"[SET-TP-LONG] 📋 Parameter:")
        logger.info(f"   • Symbol: {symbol}")
        logger.info(f"   • TP-Preis: {tp_price:.8f} USDT")
        logger.info(f"   • Position-Size: {position_size:.8f} Coins")
        logger.info(f"   • Position-Index: {position_idx} (Long)")
        logger.info("=" * 80)
        
        url = f'{self.base_url}/v5/position/trading-stop'
        recv_window = '20000'  # Erhöht für VPN-Latenz
        content_type = 'application/json'
        
        try:
            logger.info("[SET-TP-LONG] 📍 Schritt 1: Prüfe existierende TP-Orders...")
            # FIX: Prüfe zuerst, ob bereits TP-Orders existieren und canceln ALLE, bevor eine neue gesetzt wird
            # Dies verhindert mehrfache TP-Orders mit unterschiedlichen Preisen
            # WICHTIG: Prüfe MEHRMALS mit Retry, da Orders möglicherweise gerade erst gesetzt wurden (Race Condition)
            # OPTIMIERT: Retry-Delay reduziert für schnellere Order-Setzung
            # WICHTIG: Verwende fetch_open_orders_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
            max_retries = 3
            retry_delay = 0.3  # OPTIMIERT: Von 0.5s auf 0.3s reduziert
            existing_tp_orders = []
            found_exact_match = False
            
            for retry in range(max_retries):
                try:
                    logger.debug(f"[SET-TP-LONG] 📍 Schritt 1.{retry + 1}: Hole offene Orders (Versuch {retry + 1}/{max_retries})...")
                    open_orders = self.fetch_open_orders_direct(symbol, timeout=5)
                    logger.debug(f"[SET-TP-LONG] 📍 Schritt 1.{retry + 1}: {len(open_orders)} offene Orders gefunden")
                    existing_tp_orders = []
                    for order in open_orders:
                        order_info = order.get("info", {})
                        order_type = order.get("type", "")
                        stop_order_type = order_info.get("stopOrderType", "")
                        order_pos_idx = int(order_info.get("positionIdx", 0))
                        order_side = order_info.get("side", "")
                        reduce_only = order_info.get("reduceOnly", False)
                        
                        # Long-TP: Wird über trading-stop gesetzt und hat stopOrderType="PartialTakeProfit"
                        # ODER: Limit-Order, Side=Sell, PositionIdx=1, reduceOnly=True
                        is_long_tp = False
                        if stop_order_type == "PartialTakeProfit" and order_side == "Sell" and order_pos_idx == position_idx:
                            # Long-TP über trading-stop gesetzt
                            is_long_tp = True
                        elif (order_type in ("limit", "Limit") and 
                              order_side == "Sell" and 
                              order_pos_idx == position_idx and
                              reduce_only):
                            # Long-TP als normale Limit-Order
                            is_long_tp = True
                        
                        if is_long_tp:
                            existing_tp_orders.append(order)
                            # Prüfe TP-Preis (tpLimitPrice oder price)
                            existing_tp_price = None
                            if order_info.get("tpLimitPrice"):
                                existing_tp_price = float(order_info.get("tpLimitPrice"))
                            elif order.get("price") or order_info.get("price"):
                                existing_tp_price = float(order.get("price") or order_info.get("price"))
                            elif order_info.get("triggerPrice"):
                                # Für PartialTakeProfit kann triggerPrice verwendet werden
                                existing_tp_price = float(order_info.get("triggerPrice"))
                            
                            if existing_tp_price:
                                # Prüfe, ob der Preis ähnlich ist (Toleranz: 0.1%)
                                price_diff = abs(existing_tp_price - tp_price)
                                price_tolerance = tp_price * 0.001  # 0.1% Toleranz
                                
                                if price_diff < price_tolerance:
                                    logger.info("=" * 80)
                                    logger.info(f"[SET-TP-LONG] ✅ Long TP Order existiert bereits mit identischem Preis")
                                    logger.info(f"   • Existierender Preis: {existing_tp_price:.6f}")
                                    logger.info(f"   • Erwarteter Preis: {tp_price:.6f}")
                                    logger.info(f"   • Preis-Differenz: {price_diff:.6f} (< 0.1% Toleranz)")
                                    logger.info(f"   • OrderId: {order.get('id', 'N/A')}")
                                    logger.info(f"[SET-TP-LONG] ⏭️ Überspringe Setzung (Order bereits korrekt)")
                                    logger.info("=" * 80)
                                    found_exact_match = True
                                    break
                    
                    # Wenn exakte Übereinstimmung gefunden, abbrechen
                    if found_exact_match:
                        return True
                    
                    # Wenn keine Orders gefunden wurden und es nicht der letzte Versuch ist, warte kurz und versuche es erneut
                    if len(existing_tp_orders) == 0 and retry < max_retries - 1:
                        logger.debug(f"[TP-LONG] Keine TP-Orders gefunden (Versuch {retry + 1}/{max_retries}) – warte {retry_delay}s und versuche erneut...")
                        time.sleep(retry_delay)
                        continue
                    
                    # Wenn Orders gefunden wurden, breche Retry-Loop ab
                    if len(existing_tp_orders) > 0:
                        break
                        
                except Exception as e:
                    logger.warning(f"⚠️ Fehler beim Prüfen existierender Long TP-Orders (Versuch {retry + 1}/{max_retries}): {e}")
                    if retry < max_retries - 1:
                        time.sleep(retry_delay)
                    else:
                        logger.warning(f"⚠️ Alle Versuche fehlgeschlagen – setze trotzdem neue Order")
            
            # FIX: Wenn mehrere TP-Orders existieren (auch mit unterschiedlichen Preisen), canceln ALLE
            logger.info(f"[SET-TP-LONG] 📍 Schritt 2: {len(existing_tp_orders)} existierende TP-Order(s) gefunden")
            if len(existing_tp_orders) > 1:
                logger.warning("=" * 80)
                logger.warning(f"[SET-TP-LONG] ⚠️ {len(existing_tp_orders)} Long TP-Orders gefunden – cancelle ALLE vor dem Setzen einer neuen")
                logger.warning("=" * 80)
                failed_cancels = []
                for order in existing_tp_orders:
                    try:
                        order_id = order.get('id')
                        # WICHTIG: Verwende cancel_order_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
                        result = self.cancel_order_direct(order_id, symbol, timeout=5)
                        if result is not None:
                            logger.info(f"✅ Long TP-Order {order_id} gecancelt")
                        else:
                            failed_cancels.append(order_id)
                            logger.warning(f"⚠️ Long TP-Order {order_id} konnte NICHT gecancelt werden (möglicherweise noch aktiv)")
                    except Exception as e:
                        failed_cancels.append(order.get('id', 'N/A'))
                        logger.warning(f"⚠️ Fehler beim Canceln der Long TP-Order {order.get('id', 'N/A')}: {e}")
                
                # OPTIMIERT: Warte 0.5 Sekunden für schnelle Wiederherstellung (von 1.5s reduziert)
                time.sleep(0.5)
                
                # Prüfe nach fehlgeschlagenen Cancels, ob die Orders wirklich noch existieren
                if failed_cancels:
                    logger.warning(f"[SET-TP-LONG] ⚠️ {len(failed_cancels)} Cancel-Versuche fehlgeschlagen – prüfe ob Orders noch aktiv sind...")
                    remaining_orders = self.fetch_open_orders_direct(symbol, timeout=5)
                    remaining_tp_order_ids = []
                    for order in remaining_orders:
                        order_info = order.get('info', {})
                        if (int(order_info.get('positionIdx', 0)) == 1 and  # Long-Position
                            order_info.get('stopOrderType', '') in ['TakeProfit', 'PartialTakeProfit']):
                            remaining_tp_order_ids.append(order.get('id'))
                    
                    if remaining_tp_order_ids:
                        logger.error(f"[SET-TP-LONG] ❌ KRITISCH: {len(remaining_tp_order_ids)} Long-TP-Orders sind noch aktiv!")
                        logger.error(f"[SET-TP-LONG] ❌ Aktive Order-IDs: {remaining_tp_order_ids}")
                        logger.error(f"[SET-TP-LONG] ❌ Neue Order wird NICHT gesetzt, um Duplikate zu vermeiden!")
                        return None
                    else:
                        logger.info(f"[SET-TP-LONG] ✅ Alle Orders wurden erfolgreich entfernt (trotz fehlgeschlagener Cancel-Requests)")
            elif len(existing_tp_orders) == 1:
                # Eine TP-Order existiert, aber Preis ist unterschiedlich → canceln und neu setzen
                existing_order = existing_tp_orders[0]
                existing_order_id = existing_order.get('id', 'N/A')
                existing_order_info = existing_order.get('info', {})
                existing_tp_price = None
                if existing_order_info.get("tpLimitPrice"):
                    existing_tp_price = float(existing_order_info.get("tpLimitPrice"))
                elif existing_order.get("price") or existing_order_info.get("price"):
                    existing_tp_price = float(existing_order.get("price") or existing_order_info.get("price"))
                elif existing_order_info.get("triggerPrice"):
                    existing_tp_price = float(existing_order_info.get("triggerPrice"))
                
                logger.info("=" * 80)
                logger.info(f"[SET-TP-LONG] 🔄 Eine TP-Order existiert, aber Preis ist unterschiedlich")
                logger.info(f"   • Existierende OrderId: {existing_order_id}")
                if existing_tp_price:
                    logger.info(f"   • Existierender Preis: {existing_tp_price:.6f}")
                    logger.info(f"   • Neuer Preis: {tp_price:.6f}")
                    logger.info(f"   • Preis-Differenz: {abs(existing_tp_price - tp_price):.6f}")
                else:
                    logger.info(f"   • Existierender Preis: N/A")
                    logger.info(f"   • Neuer Preis: {tp_price:.6f}")
                logger.info(f"[SET-TP-LONG] 🔄 Cancelle alte Order und setze neue...")
                logger.info("=" * 80)
                try:
                    # WICHTIG: Verwende cancel_order_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
                    result = self.cancel_order_direct(existing_order_id, symbol, timeout=5)
                    if result is not None:
                        logger.info(f"[SET-TP-LONG] ✅ Long TP-Order {existing_order_id} erfolgreich gecancelt")
                        time.sleep(0.5)  # OPTIMIERT: Von 1.0s auf 0.5s reduziert
                    else:
                        logger.warning(f"[SET-TP-LONG] ⚠️ Long TP-Order {existing_order_id} konnte NICHT gecancelt werden – prüfe ob noch aktiv...")
                        # Prüfe ob Order wirklich noch existiert
                        time.sleep(0.5)  # Kurze Pause für API-Sync
                        remaining_orders = self.fetch_open_orders_direct(symbol, timeout=5)
                        order_still_exists = False
                        for order in remaining_orders:
                            if order.get('id') == existing_order_id:
                                order_still_exists = True
                                break
                        
                        if order_still_exists:
                            logger.error(f"[SET-TP-LONG] ❌ KRITISCH: Order {existing_order_id} ist noch aktiv!")
                            logger.error(f"[SET-TP-LONG] ❌ Neue Order wird NICHT gesetzt, um Duplikate zu vermeiden!")
                            return None
                        else:
                            logger.info(f"[SET-TP-LONG] ✅ Order wurde erfolgreich entfernt (trotz fehlgeschlagenem Cancel-Request)")
                except Exception as e:
                    # Race Condition: Order wurde parallel (z.B. durch Cancel-All) bereits gecancelt.
                    # Oder Order existiert nicht mehr (404/OrderNotFound)
                    error_str = str(e).lower()
                    if 'not found' in error_str or '404' in error_str or 'order not found' in error_str:
                        logger.warning(f"[SET-TP-LONG] ⚠️ Long TP-Order {existing_order_id} existiert nicht mehr/zu spät zum Canceln (Race): {e}")
                    else:
                        logger.error(f"[SET-TP-LONG] ❌ Fehler beim Canceln der Long TP-Order {existing_order_id}: {e}", exc_info=True)
                    # Prüfe trotzdem ob Order noch existiert
                    try:
                        remaining_orders = self.fetch_open_orders_direct(symbol, timeout=5)
                        order_still_exists = any(order.get('id') == existing_order_id for order in remaining_orders)
                        if order_still_exists:
                            logger.error(f"[SET-TP-LONG] ❌ Order {existing_order_id} ist noch aktiv – neue Order wird NICHT gesetzt!")
                            return None
                    except:
                        pass  # Ignoriere Fehler bei Prüfung
            else:
                logger.info(f"[SET-TP-LONG] ✅ Keine existierende TP-Order gefunden – setze neue Order")
            
            logger.info(f"[SET-TP-LONG] 📍 Schritt 3: Erstelle Request-Body...")
            request_body = {
                "category": "linear",
                "symbol": symbol,
                "position_idx": position_idx,
                "takeProfit": str(tp_price),
                "tpSize": str(position_size),
                "tpTriggerBy": trigger_by,
                "tpslMode": "Partial",
                "tpOrderType": "Market",  # Market-Order für sofortige Ausführung
                "reduceOnly": True,
                "closeOnTrigger": True,  # WICHTIG: Schließt Position, wenn Trigger-Preis erreicht wird
                "side": "Sell"
            }

            request_body_json = json.dumps(request_body)
            timestamp = str(int(time.time() * 1000))
            params_to_sign = timestamp + self.api_key + recv_window + request_body_json
            signature = hmac.new(
                self.secret_key.encode('utf-8'), params_to_sign.encode('utf-8'), hashlib.sha256
            ).hexdigest()

            headers = {
                'Content-Type': content_type,
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-RECV-WINDOW': recv_window,
                'X-BAPI-SIGN': signature,
                'X-BAPI-TIMESTAMP': timestamp
            }

            response = requests.post(url, headers=headers, data=request_body_json)
            
            if response.status_code == 200:
                response_json = response.json()
                if response_json.get("retCode") == 0:
                    # DETAILLIERTE LOGS FÜR NACHVOLLZIEHBARKEIT
                    logger.info("=" * 80)
                    logger.info("✅ LONG TP ORDER ERFOLGREICH GESETZT (Bybit API Response):")
                    logger.info(f"   Symbol: {symbol}")
                    logger.info(f"   Account: Main-Account")
                    logger.info(f"   TP-Preis: {tp_price:.8f} USDT")
                    logger.info(f"   TP-Size: {position_size:.8f} Coins")
                    logger.info(f"   Position-Index: {position_idx} (Long)")
                    logger.info(f"   TP-TriggerBy: {trigger_by}")
                    logger.info(f"   TP-OrderType: Market")
                    logger.info(f"   TP-TriggerPrice: {tp_price:.8f} USDT")
                    logger.info(f"   Side: Sell (Long-Position schließen)")
                    logger.info(f"   ReduceOnly: True")
                    logger.info(f"   CloseOnTrigger: True")
                    logger.info(f"   Bybit retCode: {response_json.get('retCode')}")
                    logger.info(f"   Bybit retMsg: {response_json.get('retMsg', 'N/A')}")
                    logger.info("=" * 80)
                    return True
                else:
                    logger.error(f"❌ Fehler beim Setzen der Long TP Order:")
                    logger.error(f"   Symbol: {symbol}")
                    logger.error(f"   TP-Preis: {tp_price:.8f} USDT")
                    logger.error(f"   TP-Size: {position_size:.8f} Coins")
                    logger.error(f"   Bybit retCode: {response_json.get('retCode')}")
                    logger.error(f"   Bybit retMsg: {response_json.get('retMsg', 'N/A')}")
                    return False
            else:
                logger.error(f"❌ HTTP-Fehler beim Setzen der Long TP Order:")
                logger.error(f"   Symbol: {symbol}")
                logger.error(f"   TP-Preis: {tp_price:.8f} USDT")
                logger.error(f"   TP-Size: {position_size:.8f} Coins")
                logger.error(f"   HTTP Status: {response.status_code}")
                logger.error(f"   Response: {response.text[:200]}")
                return False

        except Exception as e:
            logger.error("=" * 80)
            logger.error(f"[SET-TP-LONG] ❌ EXCEPTION: Fehler beim Setzen der Long TP Order")
            logger.error(f"   • Symbol: {symbol}")
            logger.error(f"   • TP-Preis: {tp_price:.8f} USDT")
            logger.error(f"   • Position-Size: {position_size:.8f} Coins")
            logger.error(f"   • Exception-Type: {type(e).__name__}")
            logger.error(f"   • Exception-Message: {str(e)}")
            logger.error("=" * 80)
            logger.error(f"[SET-TP-LONG] ❌ Stack Trace:", exc_info=True)
            logger.error("=" * 80)
            return False

    def set_tp_short_order(self, symbol, tp_price, position_size, position_idx=2, trigger_by: str = "MarkPrice"):
        """
        Setzt eine Take-Profit-Order für eine Short-Position.
        
        WICHTIG: TP und SL werden IMMER getrennt gesetzt!
        - TP wird hier gesetzt (reine TP-Order)
        - SL wird separat mit set_sl_short_order() gesetzt (reine SL-Order)
        
        WICHTIG: TriggerBy ist parametrisierbar (MarkPrice/LastPrice).
        
        :param symbol: Handelssymbol (z. B. "ETHUSDT")
        :param tp_price: Take-Profit-Preis
        :param position_size: Positionsgröße
        :param position_idx: Positionsindex (Standard: 2 für Short)
        :return: True wenn erfolgreich, False sonst
        """
        # DETAILLIERTES LOGGING: Funktion wurde aufgerufen
        logger.info("=" * 80)
        logger.info("[SET-TP-SHORT] 🚀 START: set_tp_short_order() aufgerufen")
        logger.info(f"[SET-TP-SHORT] 📋 Parameter:")
        logger.info(f"   • Symbol: {symbol}")
        logger.info(f"   • TP-Preis: {tp_price:.8f} USDT")
        logger.info(f"   • Position-Size: {position_size:.8f} Coins")
        logger.info(f"   • Position-Index: {position_idx} (Short)")
        logger.info(f"   • SL: Wird separat mit set_sl_short_order() gesetzt (reine TP-Order)")
        logger.info("=" * 80)
        
        url = f'{self.base_url}/v5/position/trading-stop'
        recv_window = '20000'  # Erhöht für VPN-Latenz
        content_type = 'application/json'
        
        try:
            logger.info("[SET-TP-SHORT] 📍 Schritt 1: Prüfe existierende TP-Orders...")
            # FIX: Prüfe zuerst, ob bereits TP-Orders existieren und canceln ALLE, bevor eine neue gesetzt wird
            # Dies verhindert mehrfache TP-Orders mit unterschiedlichen Preisen
            # WICHTIG: Prüfe MEHRMALS mit Retry, da Orders möglicherweise gerade erst gesetzt wurden (Race Condition)
            # OPTIMIERT: Retry-Delay reduziert für schnellere Order-Setzung
            # WICHTIG: Verwende fetch_open_orders_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
            max_retries = 3
            retry_delay = 0.3  # OPTIMIERT: Von 0.5s auf 0.3s reduziert
            existing_tp_orders = []
            found_exact_match = False
            
            for retry in range(max_retries):
                try:
                    logger.debug(f"[SET-TP-SHORT] 📍 Schritt 1.{retry + 1}: Hole offene Orders (Versuch {retry + 1}/{max_retries})...")
                    open_orders = self.fetch_open_orders_direct(symbol, timeout=5)
                    logger.debug(f"[SET-TP-SHORT] 📍 Schritt 1.{retry + 1}: {len(open_orders)} offene Orders gefunden")
                    existing_tp_orders = []
                    for order in open_orders:
                        order_info = order.get("info", {})
                        stop_order_type = order_info.get("stopOrderType", "")
                        order_pos_idx = int(order_info.get("positionIdx", 0))
                        order_side = order_info.get("side", "")
                        
                        # Short-TP: PartialTakeProfit, Side=Buy, PositionIdx=2
                        if stop_order_type == "PartialTakeProfit" and order_side == "Buy" and order_pos_idx == position_idx:
                            existing_tp_orders.append(order)
                            # Prüfe TP-Preis (tpLimitPrice oder price oder triggerPrice)
                            existing_tp_price = None
                            if order_info.get("tpLimitPrice"):
                                existing_tp_price = float(order_info.get("tpLimitPrice"))
                            elif order.get("price") or order_info.get("price"):
                                existing_tp_price = float(order.get("price") or order_info.get("price"))
                            elif order_info.get("triggerPrice"):
                                existing_tp_price = float(order_info.get("triggerPrice"))
                            
                            if existing_tp_price:
                                # Prüfe, ob der Preis ähnlich ist (Toleranz: 0.1%)
                                price_diff = abs(existing_tp_price - tp_price)
                                price_tolerance = tp_price * 0.001  # 0.1% Toleranz
                                
                                if price_diff < price_tolerance:
                                    logger.info("=" * 80)
                                    logger.info(f"[SET-TP-SHORT] ✅ Short TP Order existiert bereits mit identischem Preis")
                                    logger.info(f"   • Existierender Preis: {existing_tp_price:.6f}")
                                    logger.info(f"   • Erwarteter Preis: {tp_price:.6f}")
                                    logger.info(f"   • Preis-Differenz: {price_diff:.6f} (< 0.1% Toleranz)")
                                    logger.info(f"   • OrderId: {order.get('id', 'N/A')}")
                                    logger.info(f"[SET-TP-SHORT] ⏭️ Überspringe Setzung (Order bereits korrekt)")
                                    logger.info("=" * 80)
                                    found_exact_match = True
                                    break
                    
                    # Wenn exakte Übereinstimmung gefunden, abbrechen
                    if found_exact_match:
                        return True
                    
                    # Wenn keine Orders gefunden wurden und es nicht der letzte Versuch ist, warte kurz und versuche es erneut
                    if len(existing_tp_orders) == 0 and retry < max_retries - 1:
                        logger.debug(f"[TP-SHORT] Keine TP-Orders gefunden (Versuch {retry + 1}/{max_retries}) – warte {retry_delay}s und versuche erneut...")
                        time.sleep(retry_delay)
                        continue
                    
                    # Wenn Orders gefunden wurden, breche Retry-Loop ab
                    if len(existing_tp_orders) > 0:
                        break
                        
                except Exception as e:
                    logger.warning(f"⚠️ Fehler beim Prüfen existierender TP-Orders (Versuch {retry + 1}/{max_retries}): {e}")
                    if retry < max_retries - 1:
                        time.sleep(retry_delay)
                    else:
                        logger.warning(f"⚠️ Alle Versuche fehlgeschlagen – setze trotzdem neue Order")
            
            # FIX: Wenn mehrere TP-Orders existieren (auch mit unterschiedlichen Preisen), canceln ALLE
            logger.info(f"[SET-TP-SHORT] 📍 Schritt 2: {len(existing_tp_orders)} existierende TP-Order(s) gefunden")
            if len(existing_tp_orders) > 1:
                logger.warning("=" * 80)
                logger.warning(f"[SET-TP-SHORT] ⚠️ {len(existing_tp_orders)} Short TP-Orders gefunden – cancelle ALLE vor dem Setzen einer neuen")
                logger.warning("=" * 80)
                failed_cancels = []
                for order in existing_tp_orders:
                    try:
                        order_id = order.get('id')
                        # WICHTIG: Verwende cancel_order_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
                        result = self.cancel_order_direct(order_id, symbol, timeout=5)
                        if result is not None:
                            logger.info(f"✅ Short TP-Order {order_id} gecancelt")
                        else:
                            failed_cancels.append(order_id)
                            logger.warning(f"⚠️ Short TP-Order {order_id} konnte NICHT gecancelt werden (möglicherweise noch aktiv)")
                    except Exception as e:
                        failed_cancels.append(order.get('id', 'N/A'))
                        logger.warning(f"⚠️ Fehler beim Canceln der Short TP-Order {order.get('id', 'N/A')}: {e}")
                
                # Warte länger, damit alle Cancellations abgeschlossen sind
                time.sleep(1.5)
                
                # Prüfe nach fehlgeschlagenen Cancels, ob die Orders wirklich noch existieren
                if failed_cancels:
                    logger.warning(f"[SET-TP-SHORT] ⚠️ {len(failed_cancels)} Cancel-Versuche fehlgeschlagen – prüfe ob Orders noch aktiv sind...")
                    remaining_orders = self.fetch_open_orders_direct(symbol, timeout=5)
                    remaining_tp_order_ids = []
                    for order in remaining_orders:
                        order_info = order.get('info', {})
                        if (int(order_info.get('positionIdx', 0)) == position_idx and 
                            order_info.get('stopOrderType', '') in ['TakeProfit', 'PartialTakeProfit']):
                            remaining_tp_order_ids.append(order.get('id'))
                    
                    if remaining_tp_order_ids:
                        logger.error(f"[SET-TP-SHORT] ❌ KRITISCH: {len(remaining_tp_order_ids)} Short-TP-Orders sind noch aktiv!")
                        logger.error(f"[SET-TP-SHORT] ❌ Aktive Order-IDs: {remaining_tp_order_ids}")
                        logger.error(f"[SET-TP-SHORT] ❌ Neue Order wird NICHT gesetzt, um Duplikate zu vermeiden!")
                        return None
                    else:
                        logger.info(f"[SET-TP-SHORT] ✅ Alle Orders wurden erfolgreich entfernt (trotz fehlgeschlagener Cancel-Requests)")
            elif len(existing_tp_orders) == 1:
                # Eine TP-Order existiert, aber Preis ist unterschiedlich → canceln und neu setzen
                existing_order = existing_tp_orders[0]
                existing_order_id = existing_order.get('id', 'N/A')
                existing_order_info = existing_order.get('info', {})
                existing_tp_price = None
                if existing_order_info.get("tpLimitPrice"):
                    existing_tp_price = float(existing_order_info.get("tpLimitPrice"))
                elif existing_order.get("price") or existing_order_info.get("price"):
                    existing_tp_price = float(existing_order.get("price") or existing_order_info.get("price"))
                elif existing_order_info.get("triggerPrice"):
                    existing_tp_price = float(existing_order_info.get("triggerPrice"))
                
                logger.info("=" * 80)
                logger.info(f"[SET-TP-SHORT] 🔄 Eine TP-Order existiert, aber Preis ist unterschiedlich")
                logger.info(f"   • Existierende OrderId: {existing_order_id}")
                if existing_tp_price:
                    logger.info(f"   • Existierender Preis: {existing_tp_price:.6f}")
                    logger.info(f"   • Neuer Preis: {tp_price:.6f}")
                    logger.info(f"   • Preis-Differenz: {abs(existing_tp_price - tp_price):.6f}")
                else:
                    logger.info(f"   • Existierender Preis: N/A")
                    logger.info(f"   • Neuer Preis: {tp_price:.6f}")
                logger.info(f"[SET-TP-SHORT] 🔄 Cancelle alte Order und setze neue...")
                logger.info("=" * 80)
                try:
                    # WICHTIG: Verwende cancel_order_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
                    result = self.cancel_order_direct(existing_order_id, symbol, timeout=5)
                    if result is not None:
                        logger.info(f"[SET-TP-SHORT] ✅ Short TP-Order {existing_order_id} erfolgreich gecancelt")
                        time.sleep(1.0)
                    else:
                        logger.warning(f"[SET-TP-SHORT] ⚠️ Short TP-Order {existing_order_id} konnte NICHT gecancelt werden – prüfe ob noch aktiv...")
                        # Prüfe ob Order wirklich noch existiert
                        time.sleep(0.5)  # Kurze Pause für API-Sync
                        remaining_orders = self.fetch_open_orders_direct(symbol, timeout=5)
                        order_still_exists = False
                        for order in remaining_orders:
                            if order.get('id') == existing_order_id:
                                order_still_exists = True
                                break
                        
                        if order_still_exists:
                            logger.error(f"[SET-TP-SHORT] ❌ KRITISCH: Order {existing_order_id} ist noch aktiv!")
                            logger.error(f"[SET-TP-SHORT] ❌ Neue Order wird NICHT gesetzt, um Duplikate zu vermeiden!")
                            return None
                        else:
                            logger.info(f"[SET-TP-SHORT] ✅ Order wurde erfolgreich entfernt (trotz fehlgeschlagenem Cancel-Request)")
                except Exception as e:
                    logger.error(f"[SET-TP-SHORT] ❌ Fehler beim Canceln der Short TP-Order {existing_order_id}: {e}", exc_info=True)
                    # Prüfe trotzdem ob Order noch existiert
                    try:
                        remaining_orders = self.fetch_open_orders_direct(symbol, timeout=5)
                        order_still_exists = any(order.get('id') == existing_order_id for order in remaining_orders)
                        if order_still_exists:
                            logger.error(f"[SET-TP-SHORT] ❌ Order {existing_order_id} ist noch aktiv – neue Order wird NICHT gesetzt!")
                            return None
                    except:
                        pass  # Ignoriere Fehler bei Prüfung
            else:
                logger.info(f"[SET-TP-SHORT] ✅ Keine existierende TP-Order gefunden – setze neue Order")
            
            # Hole aktuellen Marktpreis, um zu prüfen, ob TP-Preis niedriger ist
            logger.info(f"[SET-TP-SHORT] 📍 Schritt 3: Erstelle Request-Body...")
            request_body = {
                "category": "linear",
                "symbol": symbol,
                "position_idx": position_idx,
                "takeProfit": str(tp_price),
                "tpSize": str(position_size),
                "tpTriggerBy": trigger_by,
                "tpslMode": "Partial",
                "tpOrderType": "Market",  # Market-Order für sofortige Ausführung
                "reduceOnly": True,
                "closeOnTrigger": True,  # WICHTIG: Schließt Position, wenn Trigger-Preis erreicht wird
                "side": "Buy"
            }
            
            # FIX: TP und SL werden IMMER getrennt gesetzt!
            # SL wird separat mit set_sl_short_order() gesetzt (reine SL-Order)
            # Keine SL-Parameter mehr hier, um Verwirrung zu vermeiden
            
            logger.debug(f"[SET-TP-SHORT] 📍 Schritt 4: Request-Body erstellt: {json.dumps(request_body, indent=2)}")

            logger.info(f"[SET-TP-SHORT] 📍 Schritt 5: Erstelle Signature und Headers...")
            request_body_json = json.dumps(request_body)
            timestamp = str(int(time.time() * 1000))
            params_to_sign = timestamp + self.api_key + recv_window + request_body_json
            signature = hmac.new(
                self.secret_key.encode('utf-8'), params_to_sign.encode('utf-8'), hashlib.sha256
            ).hexdigest()

            headers = {
                'Content-Type': content_type,
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-RECV-WINDOW': recv_window,
                'X-BAPI-SIGN': signature,
                'X-BAPI-TIMESTAMP': timestamp
            }
            
            logger.info(f"[SET-TP-SHORT] 📍 Schritt 6: Sende API-Request an Bybit...")
            logger.debug(f"[SET-TP-SHORT] 📍 Schritt 6: URL: {url}")
            logger.debug(f"[SET-TP-SHORT] 📍 Schritt 6: Timestamp: {timestamp}")
            
            response = requests.post(url, headers=headers, data=request_body_json)
            
            logger.info(f"[SET-TP-SHORT] 📍 Schritt 7: API-Response erhalten (Status Code: {response.status_code})")
            
            if response.status_code == 200:
                response_json = response.json()
                if response_json.get("retCode") == 0:
                    # DETAILLIERTE LOGS FÜR NACHVOLLZIEHBARKEIT
                    logger.info("=" * 80)
                    logger.info("✅ SHORT TP ORDER ERFOLGREICH GESETZT (Bybit API Response):")
                    logger.info(f"   Symbol: {symbol}")
                    logger.info(f"   Account: Sub-Account")
                    logger.info(f"   TP-Preis: {tp_price:.8f} USDT")
                    logger.info(f"   TP-Size: {position_size:.8f} Coins")
                    logger.info(f"   Position-Index: {position_idx} (Short)")
                    logger.info(f"   TP-TriggerBy: {trigger_by}")
                    logger.info(f"   TP-OrderType: Market")
                    logger.info(f"   TP-TriggerPrice: {tp_price:.8f} USDT")
                    logger.info(f"   Side: Buy (Short-Position schließen)")
                    logger.info(f"   ReduceOnly: True")
                    logger.info(f"   CloseOnTrigger: True")
                    logger.info(f"   Bybit retCode: {response_json.get('retCode')}")
                    logger.info(f"   Bybit retMsg: {response_json.get('retMsg', 'N/A')}")
                    logger.info("=" * 80)
                    return True
                else:
                    error_msg = response_json.get('retMsg', 'Unknown error')
                    ret_code = response_json.get('retCode', 'Unknown')
                    logger.error(f"❌ Fehler beim Setzen der Short TP Order:")
                    logger.error(f"   Symbol: {symbol}")
                    logger.error(f"   TP-Preis: {tp_price:.8f} USDT")
                    logger.error(f"   TP-Size: {position_size:.8f} Coins")
                    logger.error(f"   TP-TriggerBy: {trigger_by}")
                    logger.error(f"   Bybit retCode: {ret_code}")
                    logger.error(f"   Bybit retMsg: {error_msg}")
                    
                    return False
            else:
                logger.error(f"HTTP-Fehler beim Setzen der Short TP Order: {response.status_code}")
                return False

        except Exception as e:
            logger.error("=" * 80)
            logger.error(f"[SET-TP-SHORT] ❌ EXCEPTION: Fehler beim Setzen der Short TP Order")
            logger.error(f"   • Symbol: {symbol}")
            logger.error(f"   • TP-Preis: {tp_price:.8f} USDT")
            logger.error(f"   • Position-Size: {position_size:.8f} Coins")
            logger.error(f"   • Exception-Type: {type(e).__name__}")
            logger.error(f"   • Exception-Message: {str(e)}")
            logger.error("=" * 80)
            logger.error(f"[SET-TP-SHORT] ❌ Stack Trace:", exc_info=True)
            logger.error("=" * 80)
            return False

    def set_sl_short_order(self, symbol, sl_price, position_size, position_idx=2, trigger_by: str = "LastPrice"):
        """
        Setzt eine Stop-Loss-Order für eine Short-Position.
        Wird ausgelöst, wenn der Preis auf oder über sl_price STEIGT (Stop-Loss für Short).
        
        :param symbol: Handelssymbol (z. B. "ETHUSDT")
        :param sl_price: Stop-Loss-Preis (Trigger-Preis)
        :param position_size: Positionsgröße
        :param position_idx: Positionsindex (Standard: 2 für Short)
        :return: True wenn erfolgreich, False sonst
        """
        url = f'{self.base_url}/v5/position/trading-stop'
        recv_window = '20000'  # Erhöht für VPN-Latenz
        content_type = 'application/json'
        
        try:
            # WICHTIG: Runde Size auf korrekte Präzision (wie in anderen Methoden)
            qty_step, min_qty = self.get_symbol_info(symbol)
            if qty_step:
                # Runde Size auf nächstes Vielfaches von qtyStep
                raw_size = float(position_size)
                rounded_size = round(raw_size / qty_step) * qty_step
                if rounded_size < min_qty:
                    rounded_size = min_qty
                # Stelle sicher, dass keine überflüssigen Dezimalstellen vorhanden sind
                decimals = len(str(qty_step).split('.')[-1]) if '.' in str(qty_step) else 0
                position_size = round(rounded_size, decimals)
                logger.debug(f"[SET-SL-SHORT] Size gerundet: {raw_size:.6f} → {position_size:.6f} (qtyStep: {qty_step})")
            else:
                position_size = round(float(position_size), 2)
                logger.debug(f"[SET-SL-SHORT] Size gerundet (Fallback): {position_size:.6f}")
            
            request_body = {
                "category": "linear",
                "symbol": symbol,
                "position_idx": position_idx,
                "stopLoss": str(sl_price),
                "slSize": str(position_size),
                "slTriggerBy": trigger_by,
                "tpslMode": "Partial",
                "slOrderType": "Market",  # Market-Order für sofortige Ausführung
                "reduceOnly": True,
                "closeOnTrigger": True,  # WICHTIG: Schließt Position, wenn Trigger-Preis erreicht wird
                "side": "Buy"  # Buy um Short-Position zu schließen
            }

            request_body_json = json.dumps(request_body)
            timestamp = str(int(time.time() * 1000))
            params_to_sign = timestamp + self.api_key + recv_window + request_body_json
            signature = hmac.new(
                self.secret_key.encode('utf-8'), params_to_sign.encode('utf-8'), hashlib.sha256
            ).hexdigest()

            headers = {
                'Content-Type': content_type,
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-RECV-WINDOW': recv_window,
                'X-BAPI-SIGN': signature,
                'X-BAPI-TIMESTAMP': timestamp
            }

            response = requests.post(url, headers=headers, data=request_body_json)
            
            if response.status_code == 200:
                response_json = response.json()
                if response_json.get("retCode") == 0:
                    logger.info(f"Short SL Order erfolgreich gesetzt: {sl_price} für Symbol {symbol}, Size: {position_size}")
                    return True
                else:
                    logger.error(f"Fehler beim Setzen der Short SL Order: {response_json.get('retMsg')} (retCode: {response_json.get('retCode')})")
                    return False
            else:
                logger.error(f"HTTP-Fehler beim Setzen der Short SL Order: {response.status_code}")
                return False

        except Exception as e:
            logger.error(f"Fehler beim Setzen der Short SL Order: {e}", exc_info=True)
            return False

    def set_sl_long_order(self, symbol, sl_price, position_size, position_idx=1, trigger_by: str = "LastPrice"):
        """
        Setzt eine Stop-Loss-Order für eine Long-Position.
        Wird ausgelöst, wenn der Preis auf oder unter sl_price FÄLLT (Stop-Loss für Long).
        
        :param symbol: Handelssymbol (z. B. "ETHUSDT")
        :param sl_price: Stop-Loss-Preis (Trigger-Preis)
        :param position_size: Positionsgröße (in Coins)
        :param position_idx: Positionsindex (Standard: 1 für Long)
        :return: True wenn erfolgreich, False sonst
        """
        url = f'{self.base_url}/v5/position/trading-stop'
        recv_window = '20000'  # Erhöht für VPN-Latenz
        content_type = 'application/json'
        
        try:
            request_body = {
                "category": "linear",
                "symbol": symbol,
                "position_idx": position_idx,
                "stopLoss": str(sl_price),
                "slSize": str(position_size),
                "slTriggerBy": trigger_by,
                "tpslMode": "Partial",
                "slOrderType": "Market",  # Market-Order für sofortige Ausführung
                "reduceOnly": True,
                "closeOnTrigger": True,  # WICHTIG: Schließt Position, wenn Trigger-Preis erreicht wird
                "side": "Sell"  # Sell um Long-Position zu schließen
            }

            request_body_json = json.dumps(request_body)
            timestamp = str(int(time.time() * 1000))
            params_to_sign = timestamp + self.api_key + recv_window + request_body_json
            signature = hmac.new(
                self.secret_key.encode('utf-8'), params_to_sign.encode('utf-8'), hashlib.sha256
            ).hexdigest()

            headers = {
                'Content-Type': content_type,
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-RECV-WINDOW': recv_window,
                'X-BAPI-SIGN': signature,
                'X-BAPI-TIMESTAMP': timestamp
            }

            response = requests.post(url, headers=headers, data=request_body_json)
            
            if response.status_code == 200:
                response_json = response.json()
                if response_json.get("retCode") == 0:
                    logger.info(f"Long SL Order erfolgreich gesetzt: {sl_price} für Symbol {symbol}, Size: {position_size}")
                    return True
                else:
                    logger.error(f"Fehler beim Setzen der Long SL Order: {response_json.get('retMsg')} (retCode: {response_json.get('retCode')})")
                    return False
            else:
                logger.error(f"HTTP-Fehler beim Setzen der Long SL Order: {response.status_code}")
                return False

        except Exception as e:
            logger.error(f"Fehler beim Setzen der Long SL Order: {e}", exc_info=True)
            return False

    def cancel_all_limit_orders(self, symbol):
        """
        Storniert alle Limit Orders für das angegebene Symbol.
        
        :param symbol: Handelssymbol (z. B. "ETHUSDT")
        """
        try:
            # WICHTIG: Verwende fetch_open_orders_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
            open_orders = self.fetch_open_orders_direct(symbol, timeout=5)
            limit_orders = [order for order in open_orders if order['type'] == 'limit']
            
            if not limit_orders:
                logger.info(f"Keine Limit Orders für {symbol} gefunden.")
                return

            logger.info(f"Gefundene Limit Orders: {len(limit_orders)}")
            for order in limit_orders:
                try:
                    # WICHTIG: Verwende cancel_order_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
                    self.cancel_order_direct(order['id'], symbol, timeout=5)
                    logger.info(f"Limit Order {order['id']} wurde storniert.")
                except Exception as e:
                    logger.error(f"Fehler beim Stornieren der Order {order['id']}: {e}", exc_info=True)

            logger.info(f"Alle Limit Orders für {symbol} wurden storniert.")

        except Exception as e:
            logger.error(f"Fehler beim Abrufen der Orders: {e}", exc_info=True)

    def cancel_tp_orders(self, symbol):
        """
        Storniert alle Take-Profit-Orders für das angegebene Symbol.
        WICHTIG: Cancelt NUR Take-Profit-Orders, NICHT Stop-Loss-Orders!
        
        :param symbol: Handelssymbol (z. B. "ETHUSDT")
        """
        try:
            logger.info(f"[CANCEL-TP] Hole alle offenen Orders für {symbol}...")
            # WICHTIG: Verwende fetch_open_orders_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
            open_orders = self.fetch_open_orders_direct(symbol, timeout=5)
            logger.info(f"[CANCEL-TP] Gefundene offene Orders: {len(open_orders)}")
            
            # DEBUG: Logge alle Orders mit Details
            logger.info("[CANCEL-TP] 📋 Alle offenen Orders (Details):")
            for order in open_orders:
                order_id = order.get('id', 'N/A')
                stop_order_type = order.get('info', {}).get('stopOrderType', 'N/A')
                side = order.get('info', {}).get('side', 'N/A')
                order_type = order.get('type', 'N/A')
                trigger_price = order.get('info', {}).get('triggerPrice', 'N/A')
                size = order.get('info', {}).get('qty', order.get('amount', 'N/A'))
                logger.info(f"  • OrderId: {order_id}, stopOrderType: {stop_order_type}, side: {side}, type: {order_type}, triggerPrice: {trigger_price}, size: {size}")
            
            # NUR Take-Profit-Orders filtern - Stop-Loss-Orders AUSSCHLIESSEN!
            # WICHTIG: Long-SL für Burn wird von Bybit als "PartialTakeProfit" klassifiziert, obwohl sie eine Stop-Loss-Order ist!
            # Wir erkennen sie daran, dass sie:
            # - stopOrderType: "PartialTakeProfit" hat
            # - side: "Buy" hat (um Long-Position zu schließen)
            # - positionIdx: 1 hat (Long-Position)
            # FIX: Short-TP hat auch side="Buy", aber positionIdx=2 (Short-Position)!
            # Daher müssen wir positionIdx prüfen, um zwischen Short-TP und Long-SL für Burn zu unterscheiden!
            tp_orders = []
            for order in open_orders:
                stop_order_type = order.get('info', {}).get('stopOrderType', '')
                side = order.get('info', {}).get('side', 'N/A')
                order_type = order.get('type', 'N/A')
                order_id = order.get('id', 'N/A')
                position_idx = int(order.get('info', {}).get('positionIdx', 0))
                
                # Echte TP-Orders:
                # 1. "TakeProfit" (vollständiger TP) - immer TP
                # 2. "PartialTakeProfit" mit side="Sell" - Long-TP (Long-Position schließen, positionIdx=1)
                # 3. "PartialTakeProfit" mit side="Buy" UND positionIdx=2 - Short-TP (Short-Position schließen)
                # NICHT: "PartialTakeProfit" mit side="Buy" UND positionIdx=1 - Long-SL für Burn!
                if stop_order_type == 'TakeProfit':
                    tp_orders.append(order)
                    logger.info(f"[CANCEL-TP] ✅ TP-Order gefunden: {order_id} (Type: {stop_order_type}, Side: {side})")
                elif stop_order_type == 'PartialTakeProfit' and side == 'Sell':
                    # Long-TP (Long-Position schließen) - echte TP-Order
                    tp_orders.append(order)
                    logger.info(f"[CANCEL-TP] ✅ TP-Order gefunden: {order_id} (Type: {stop_order_type}, Side: {side}, PositionIdx: {position_idx})")
                elif stop_order_type == 'PartialTakeProfit' and side == 'Buy' and position_idx == 2:
                    # Short-TP (Short-Position schließen) - echte TP-Order
                    tp_orders.append(order)
                    logger.info(f"[CANCEL-TP] ✅ TP-Order gefunden: {order_id} (Type: {stop_order_type}, Side: {side}, PositionIdx: {position_idx})")
                elif stop_order_type == 'PartialTakeProfit' and side == 'Buy' and position_idx == 1:
                    # Long-SL für Burn wird als PartialTakeProfit klassifiziert, aber ist eigentlich eine Stop-Loss-Order!
                    # Diese sollten wir NICHT canceln, da sie bereits über cancel_sl_long_order() gecancelt wurde
                    logger.info(f"[CANCEL-TP] ⚠️ Überspringe Long-SL für Burn: {order_id} (Type: {stop_order_type}, Side: {side}, PositionIdx: {position_idx}) - sollte bereits gecancelt sein")

            if not tp_orders:
                logger.info(f"[CANCEL-TP] Keine TP Orders für {symbol} gefunden.")
                return

            logger.info(f"[CANCEL-TP] Gefundene TP Orders: {len(tp_orders)}")
            for order in tp_orders:
                try:
                    order_id = order['id']
                    stop_order_type = order.get('info', {}).get('stopOrderType', 'N/A')
                    side = order.get('info', {}).get('side', 'N/A')
                    logger.info(f"[CANCEL-TP] Cancelle TP Order {order_id} (Type: {stop_order_type}, Side: {side})")
                    # WICHTIG: Verwende cancel_order_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
                    self.cancel_order_direct(order_id, symbol, timeout=5)
                    logger.info(f"[CANCEL-TP] ✅ TP Order {order_id} wurde storniert.")
                except Exception as e:
                    logger.error(f"[CANCEL-TP] ❌ Fehler beim Stornieren der TP Order {order['id']}: {e}", exc_info=True)

            logger.info(f"[CANCEL-TP] ✅ Alle TP Orders für {symbol} wurden storniert.")

        except Exception as e:
            logger.error(f"[CANCEL-TP] ❌ Fehler beim Stornieren der TP Orders: {e}", exc_info=True)
    
    def cancel_short_tp_orders(self, symbol, position_idx=2):
        """
        Storniert NUR Short-TP-Orders für das angegebene Symbol.
        WICHTIG: Long-TP-Orders bleiben unberührt!
        
        Filter-Kriterien:
        - positionIdx == 2 (Short-Position)
        - stopOrderType in ["TakeProfit", "PartialTakeProfit"]
        
        :param symbol: Handelssymbol (z. B. "ETHUSDT")
        :param position_idx: Position-Index (Standard: 2 für Short)
        """
        try:
            logger.info(f"[CANCEL-SHORT-TP] Hole alle offenen Orders für {symbol}...")
            # WICHTIG: Verwende fetch_open_orders_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
            open_orders = self.fetch_open_orders_direct(symbol, timeout=5)
            logger.info(f"[CANCEL-SHORT-TP] Gefundene offene Orders: {len(open_orders)}")
            
            # Nur Short-TP-Orders filtern
            short_tp_orders = []
            for order in open_orders:
                order_info = order.get('info', {})
                stop_order_type = order_info.get('stopOrderType', '')
                order_position_idx = int(order_info.get('positionIdx', 0))
                order_id = order.get('id', 'N/A')
                
                # Nur Short-TP-Orders (positionIdx=2) mit TakeProfit-Typ
                if order_position_idx == position_idx and stop_order_type in ['TakeProfit', 'PartialTakeProfit']:
                    short_tp_orders.append(order)
                    logger.info(f"[CANCEL-SHORT-TP] ✅ Short-TP-Order gefunden: {order_id} (Type: {stop_order_type}, PositionIdx: {order_position_idx})")
            
            if not short_tp_orders:
                logger.info(f"[CANCEL-SHORT-TP] Keine Short-TP-Orders für {symbol} gefunden.")
                return
            
            logger.info(f"[CANCEL-SHORT-TP] Gefundene Short-TP-Orders: {len(short_tp_orders)}")
            cancelled_count = 0
            failed_count = 0
            
            for order in short_tp_orders:
                try:
                    order_id = order['id']
                    stop_order_type = order.get('info', {}).get('stopOrderType', 'N/A')
                    logger.info(f"[CANCEL-SHORT-TP] Cancelle Short-TP-Order {order_id} (Type: {stop_order_type})")
                    # WICHTIG: Verwende cancel_order_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
                    result = self.cancel_order_direct(order_id, symbol, timeout=5)
                    
                    # Prüfe ob Cancel erfolgreich war (nur wenn nicht None)
                    if result is not None:
                        cancelled_count += 1
                        logger.info(f"[CANCEL-SHORT-TP] ✅ Short-TP-Order {order_id} wurde erfolgreich storniert.")
                    else:
                        failed_count += 1
                        logger.warning(f"[CANCEL-SHORT-TP] ⚠️ Short-TP-Order {order_id} konnte NICHT storniert werden (möglicherweise noch aktiv)")
                except Exception as e:
                    failed_count += 1
                    logger.error(f"[CANCEL-SHORT-TP] ❌ Fehler beim Stornieren der Short-TP-Order {order['id']}: {e}", exc_info=True)
            
            # Zusammenfassung
            if failed_count == 0:
                logger.info(f"[CANCEL-SHORT-TP] ✅ Alle {cancelled_count} Short-TP-Orders für {symbol} wurden erfolgreich storniert.")
            elif cancelled_count > 0:
                logger.warning(f"[CANCEL-SHORT-TP] ⚠️ {cancelled_count} Short-TP-Orders erfolgreich storniert, {failed_count} fehlgeschlagen")
            else:
                logger.error(f"[CANCEL-SHORT-TP] ❌ Keine Short-TP-Orders konnten storniert werden ({failed_count} fehlgeschlagen)")

        except Exception as e:
            logger.error(f"[CANCEL-TP] ❌ Fehler beim Abrufen der Orders: {e}", exc_info=True)

    def cancel_sl_long_order(self, symbol, position_idx=1):
        """
        Entfernt den Long Stop-Loss explizit, ohne den Short-SL zu beeinträchtigen.
        Verwendet Bybit's trading-stop Endpoint mit leerem stopLoss-Wert.
        
        :param symbol: Handelssymbol (z. B. "ETHUSDT")
        :param position_idx: Positionsindex (Standard: 1 für Long)
        :return: True wenn erfolgreich, False sonst
        """
        url = f'{self.base_url}/v5/position/trading-stop'
        recv_window = '20000'  # Erhöht für VPN-Latenz
        content_type = 'application/json'
        
        logger.info(f"[CANCEL-SL-LONG] Cancelle Long-SL für {symbol}, position_idx={position_idx}")
        
        try:
            # Leerer stopLoss-Wert entfernt den SL
            request_body = {
                "category": "linear",
                "symbol": symbol,
                "position_idx": position_idx,
                "stopLoss": ""  # Leerer String entfernt den SL
            }

            request_body_json = json.dumps(request_body)
            timestamp = str(int(time.time() * 1000))
            params_to_sign = timestamp + self.api_key + recv_window + request_body_json
            signature = hmac.new(
                self.secret_key.encode('utf-8'), params_to_sign.encode('utf-8'), hashlib.sha256
            ).hexdigest()

            headers = {
                'Content-Type': content_type,
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-RECV-WINDOW': recv_window,
                'X-BAPI-SIGN': signature,
                'X-BAPI-TIMESTAMP': timestamp
            }

            response = requests.post(url, headers=headers, data=request_body_json)
            
            logger.info(f"[CANCEL-SL-LONG] Response Status: {response.status_code}")
            logger.info(f"[CANCEL-SL-LONG] Response Body: {response.text}")
            
            if response.status_code == 200:
                response_json = response.json()
                if response_json.get("retCode") == 0:
                    logger.info(f"[CANCEL-SL-LONG] ✅ Long SL Order erfolgreich entfernt für Symbol {symbol}, PositionIdx: {position_idx}")
                    # Kurze Verzögerung, damit die Order vollständig gecancelt wird
                    time.sleep(0.5)
                    return True
                else:
                    # Wenn kein SL vorhanden war, ist das auch OK
                    if response_json.get("retCode") == 10001:  # Invalid parameter
                        logger.info(f"[CANCEL-SL-LONG] ℹ️ Kein Long SL vorhanden für {symbol} (PositionIdx: {position_idx})")
                        return True
                    logger.warning(f"[CANCEL-SL-LONG] ⚠️ Warnung beim Entfernen der Long SL Order: {response_json.get('retMsg')} (retCode: {response_json.get('retCode')})")
                    return False
            else:
                logger.error(f"[CANCEL-SL-LONG] ❌ HTTP-Fehler beim Entfernen der Long SL Order: {response.status_code}")
                return False

        except Exception as e:
            logger.error(f"Fehler beim Entfernen der Long SL Order: {e}", exc_info=True)
            return False

    def cancel_sl_short_order(self, symbol, position_idx=2):
        """
        Entfernt den Short Stop-Loss explizit, ohne den Long-SL zu beeinträchtigen.
        Verwendet Bybit's trading-stop Endpoint mit leerem stopLoss-Wert.

        Warum eigener Endpoint-Call?
        - cancel_all_sl_orders() arbeitet über fetch_open_orders + Heuristiken und ist primär auf Long-SL (posIdx=1) ausgerichtet.
        - Für Short-SL (posIdx=2) ist der trading-stop Call deterministisch und kann nicht versehentlich TP-Orders anfassen.
        """
        url = f'{self.base_url}/v5/position/trading-stop'
        recv_window = '20000'
        content_type = 'application/json'

        logger.info(f"[CANCEL-SL-SHORT] Cancelle Short-SL für {symbol}, position_idx={position_idx}")

        try:
            request_body = {
                "category": "linear",
                "symbol": symbol,
                "position_idx": position_idx,
                "stopLoss": ""  # Leerer String entfernt den SL
            }

            request_body_json = json.dumps(request_body)
            timestamp = str(int(time.time() * 1000))
            params_to_sign = timestamp + self.api_key + recv_window + request_body_json
            signature = hmac.new(
                self.secret_key.encode('utf-8'), params_to_sign.encode('utf-8'), hashlib.sha256
            ).hexdigest()

            headers = {
                'Content-Type': content_type,
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-RECV-WINDOW': recv_window,
                'X-BAPI-SIGN': signature,
                'X-BAPI-TIMESTAMP': timestamp
            }

            response = requests.post(url, headers=headers, data=request_body_json)
            logger.info(f"[CANCEL-SL-SHORT] Response Status: {response.status_code}")
            logger.debug(f"[CANCEL-SL-SHORT] Response Body: {response.text}")

            if response.status_code == 200:
                response_json = response.json()
                if response_json.get("retCode") == 0:
                    logger.info(f"[CANCEL-SL-SHORT] ✅ Short SL erfolgreich entfernt für {symbol}, PositionIdx: {position_idx}")
                    time.sleep(0.5)
                    return True
                else:
                    # Wenn kein SL vorhanden war, ist das auch OK
                    if response_json.get("retCode") == 10001:
                        logger.info(f"[CANCEL-SL-SHORT] ℹ️ Kein Short SL vorhanden für {symbol} (PositionIdx: {position_idx})")
                        return True
                    logger.warning(f"[CANCEL-SL-SHORT] ⚠️ Warnung beim Entfernen der Short SL: {response_json.get('retMsg')} (retCode: {response_json.get('retCode')})")
                    return False
            else:
                logger.error(f"[CANCEL-SL-SHORT] ❌ HTTP-Fehler beim Entfernen der Short SL: {response.status_code}")
                return False

        except Exception as e:
            logger.error(f"Fehler beim Entfernen der Short SL Order: {e}", exc_info=True)
            return False

    def cancel_all_sl_orders(self, symbol, position_idx=None):
        """
        Cancelt ALLE Stop-Loss-Orders für das angegebene Symbol, indem es sie über ihre Order-ID canceln.
        Dies funktioniert auch für Long-SL für Burn, die als "PartialTakeProfit" klassifiziert werden.
        
        :param symbol: Handelssymbol (z. B. "ETHUSDT")
        :param position_idx: Optional - Position Index (1 für Long, 2 für Short). Wenn None, werden alle SL-Orders gecancelt.
        """
        try:
            logger.info(f"[CANCEL-ALL-SL] Hole alle offenen Orders für {symbol}...")
            # WICHTIG: Verwende fetch_open_orders_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
            open_orders = self.fetch_open_orders_direct(symbol, timeout=5)
            logger.info(f"[CANCEL-ALL-SL] Gefundene offene Orders: {len(open_orders)}")
            
            # WICHTIG: Logge ALLE gefundenen Orders für Debugging
            logger.info(f"[CANCEL-ALL-SL] 📋 Alle gefundenen Orders (Details):")
            for order in open_orders:
                order_id = order.get('id', 'N/A')
                stop_order_type = order.get('info', {}).get('stopOrderType', 'N/A')
                side = order.get('info', {}).get('side', 'N/A')
                order_pos_idx_log = int(order.get('info', {}).get('positionIdx', 0))  # FIX: Verwende order_pos_idx_log statt position_idx
                trigger_price = order.get('info', {}).get('triggerPrice', 'N/A')
                size = order.get('info', {}).get('qty', order.get('amount', 'N/A'))
                logger.info(f"  • OrderId: {order_id}, stopOrderType: {stop_order_type}, side: {side}, positionIdx: {order_pos_idx_log}, triggerPrice: {trigger_price}, size: {size}")
            
            # Finde alle SL-Orders:
            # 1. PartialStopLoss (echte SL-Orders)
            # 2. PartialTakeProfit mit side="Buy" (Long-SL für Burn, wird fälschlicherweise als TP klassifiziert)
            # 3. StopLoss (ohne "Partial" - könnte alte Orders sein)
            sl_orders = []
            for order in open_orders:
                stop_order_type = order.get('info', {}).get('stopOrderType', '')
                side = order.get('info', {}).get('side', 'N/A')
                order_id = order.get('id', 'N/A')
                trigger_price = order.get('info', {}).get('triggerPrice', 'N/A')
                size = order.get('info', {}).get('qty', order.get('amount', 'N/A'))
                order_pos_idx = int(order.get('info', {}).get('positionIdx', 0))  # FIX: Verwende order_pos_idx statt position_idx
                
                # Echte SL-Orders (PartialStopLoss oder StopLoss)
                if stop_order_type in ['StopLoss', 'PartialStopLoss']:
                    # WICHTIG: Prüfe positionIdx basierend auf Parameter
                    if position_idx is None:
                        # Kein Filter: Cancelle alle SL-Orders (Long und Short)
                        if (side == 'Sell' and order_pos_idx == 1) or (side == 'Buy' and order_pos_idx == 2):
                            sl_orders.append(order)
                            logger.info(f"[CANCEL-ALL-SL] ✅ SL-Order gefunden: {order_id} (Type: {stop_order_type}, Side: {side}, PositionIdx: {order_pos_idx}, Trigger: {trigger_price}, Size: {size})")
                    elif position_idx == 1:
                        # Nur Long-SL (Sell um Long zu schließen)
                        if side == 'Sell' and order_pos_idx == 1:
                            sl_orders.append(order)
                            logger.info(f"[CANCEL-ALL-SL] ✅ Long-SL-Order gefunden: {order_id} (Type: {stop_order_type}, Side: {side}, PositionIdx: {order_pos_idx}, Trigger: {trigger_price}, Size: {size})")
                        else:
                            logger.debug(f"[CANCEL-ALL-SL] ⏭️ Überspringe SL-Order (nicht Long-SL): {order_id} (Type: {stop_order_type}, Side: {side}, PositionIdx: {order_pos_idx})")
                    elif position_idx == 2:
                        # Nur Short-SL (Buy um Short zu schließen)
                        if side == 'Buy' and order_pos_idx == 2:
                            sl_orders.append(order)
                            logger.info(f"[CANCEL-ALL-SL] ✅ Short-SL-Order gefunden: {order_id} (Type: {stop_order_type}, Side: {side}, PositionIdx: {order_pos_idx}, Trigger: {trigger_price}, Size: {size})")
                        else:
                            logger.debug(f"[CANCEL-ALL-SL] ⏭️ Überspringe SL-Order (nicht Short-SL): {order_id} (Type: {stop_order_type}, Side: {side}, PositionIdx: {order_pos_idx})")
                # Long-SL für Burn (wird als PartialTakeProfit klassifiziert)
                # WICHTIG: Nur canceln wenn positionIdx == 1 (Long-Position), nicht positionIdx == 2 (Short-Position/Short-TP)!
                elif stop_order_type == 'PartialTakeProfit' and side == 'Buy':
                    if position_idx is None or position_idx == 1:
                        if order_pos_idx == 1:  # Nur Long-SL für Burn (Long-Position), nicht Short-TP!
                            sl_orders.append(order)
                            logger.info(f"[CANCEL-ALL-SL] ✅ Long-SL für Burn gefunden: {order_id} (Type: {stop_order_type}, Side: {side}, PositionIdx: {order_pos_idx}, Trigger: {trigger_price}, Size: {size})")
                        else:
                            logger.debug(f"[CANCEL-ALL-SL] ⏭️ Überspringe Short-TP Order: {order_id} (Type: {stop_order_type}, Side: {side}, PositionIdx: {order_pos_idx}) - nicht für Canceln")
            
            if not sl_orders:
                logger.warning(f"[CANCEL-ALL-SL] ⚠️ Keine SL Orders für {symbol} gefunden (aber {len(open_orders)} offene Orders vorhanden).")
                logger.warning(f"[CANCEL-ALL-SL] ⚠️ Möglicherweise gibt es Long-SL Orders, die nicht erkannt wurden!")
                return
            
            logger.info(f"[CANCEL-ALL-SL] Gefundene SL Orders: {len(sl_orders)}")
            for order in sl_orders:
                try:
                    order_id = order['id']
                    stop_order_type = order.get('info', {}).get('stopOrderType', 'N/A')
                    side = order.get('info', {}).get('side', 'N/A')
                    logger.info(f"[CANCEL-ALL-SL] Cancelle SL Order {order_id} (Type: {stop_order_type}, Side: {side})")
                    # WICHTIG: Verwende cancel_order_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
                    self.cancel_order_direct(order_id, symbol, timeout=5)
                    logger.info(f"[CANCEL-ALL-SL] ✅ SL Order {order_id} wurde storniert.")
                except Exception as e:
                    logger.error(f"[CANCEL-ALL-SL] ❌ Fehler beim Stornieren der SL Order {order['id']}: {e}", exc_info=True)
            
            logger.info(f"[CANCEL-ALL-SL] ✅ Alle SL Orders für {symbol} wurden storniert.")
            # Kurze Verzögerung, damit die Orders vollständig gecancelt werden
            time.sleep(0.5)

        except Exception as e:
            logger.error(f"[CANCEL-ALL-SL] ❌ Fehler beim Abrufen der Orders: {e}", exc_info=True)

    def cancel_all_tp_sl_orders(self, symbol):
        """
        Cancelt ALLE Take-Profit und Stop-Loss Orders für das angegebene Symbol.
        Dies ist die einfachste und zuverlässigste Methode - wir canceln einfach alles
        und setzen dann neu, anstatt einzelne Orders zu identifizieren.
        
        :param symbol: Handelssymbol (z. B. "ETHUSDT")
        """
        try:
            logger.info(f"[CANCEL-ALL-TP-SL] Hole alle offenen Orders für {symbol}...")
            # WICHTIG: Verwende fetch_open_orders_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
            open_orders = self.fetch_open_orders_direct(symbol, timeout=5)
            logger.info(f"[CANCEL-ALL-TP-SL] Gefundene offene Orders: {len(open_orders)}")
            
            if not open_orders:
                logger.info(f"[CANCEL-ALL-TP-SL] Keine offenen Orders für {symbol} gefunden.")
                return
            
            # Filtere nur TP und SL Orders (alle anderen Orders ignorieren wir)
            tp_sl_orders = []
            for order in open_orders:
                stop_order_type = order.get('info', {}).get('stopOrderType', '')
                order_id = order.get('id', 'N/A')
                side = order.get('info', {}).get('side', 'N/A')
                trigger_price = order.get('info', {}).get('triggerPrice', 'N/A')
                size = order.get('info', {}).get('qty', order.get('amount', 'N/A'))
                
                # Alle TP und SL Orders (inkl. Long-SL für Burn, die als PartialTakeProfit klassifiziert werden)
                if stop_order_type in ['TakeProfit', 'PartialTakeProfit', 'StopLoss', 'PartialStopLoss']:
                    tp_sl_orders.append(order)
                    logger.info(f"[CANCEL-ALL-TP-SL] ✅ Order gefunden: {order_id} (Type: {stop_order_type}, Side: {side}, Trigger: {trigger_price}, Size: {size})")
            
            if not tp_sl_orders:
                logger.info(f"[CANCEL-ALL-TP-SL] Keine TP/SL Orders für {symbol} gefunden.")
                return
            
            logger.info(f"[CANCEL-ALL-TP-SL] Gefundene TP/SL Orders: {len(tp_sl_orders)}")
            for order in tp_sl_orders:
                try:
                    order_id = order['id']
                    stop_order_type = order.get('info', {}).get('stopOrderType', 'N/A')
                    side = order.get('info', {}).get('side', 'N/A')
                    logger.info(f"[CANCEL-ALL-TP-SL] Cancelle Order {order_id} (Type: {stop_order_type}, Side: {side})")
                    # WICHTIG: Verwende cancel_order_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
                    self.cancel_order_direct(order_id, symbol, timeout=5)
                    logger.info(f"[CANCEL-ALL-TP-SL] ✅ Order {order_id} wurde storniert.")
                except Exception as e:
                    logger.error(f"[CANCEL-ALL-TP-SL] ❌ Fehler beim Stornieren der Order {order['id']}: {e}", exc_info=True)
            
            logger.info(f"[CANCEL-ALL-TP-SL] ✅ Alle TP/SL Orders für {symbol} wurden storniert.")
            # Kurze Verzögerung, damit die Orders vollständig gecancelt werden
            time.sleep(0.5)

        except Exception as e:
            logger.error(f"[CANCEL-ALL-TP-SL] ❌ Fehler beim Abrufen der Orders: {e}", exc_info=True)

    def cancel_all_orders_complete(self, symbol):
        """
        Cancelt ALLE Orders (inkl. Stop-Orders) für ein Symbol.
        Verwendet /v5/order/cancel-all ohne orderFilter,
        damit ALLE Arten von Orders gecancelt werden.
        
        WICHTIG: Dieser Endpoint canceln ALLE Orders:
        - Active Orders
        - Conditional Orders
        - TP/SL Orders
        - Trailing Stop Orders
        
        :param symbol: Trading-Symbol (z.B. "XPINUSDT")
        :return: True wenn erfolgreich, False sonst
        """
        url = f'{self.base_url}/v5/order/cancel-all'
        recv_window = '20000'  # Erhöht für VPN-Latenz
        content_type = 'application/json'
        
        logger.info(f"[CANCEL-ALL-COMPLETE] 🧹 Cancelle ALLE Orders für {symbol}...")
        
        try:
            # orderFilter NICHT übergeben → Cancelt ALLES (inkl. Stop-Orders)!
            request_body = {
                "category": "linear",
                "symbol": symbol
            }
            
            request_body_json = json.dumps(request_body)
            timestamp = str(int(time.time() * 1000))
            params_to_sign = timestamp + self.api_key + recv_window + request_body_json
            signature = hmac.new(
                self.secret_key.encode('utf-8'), params_to_sign.encode('utf-8'), hashlib.sha256
            ).hexdigest()
            
            headers = {
                'Content-Type': content_type,
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-RECV-WINDOW': recv_window,
                'X-BAPI-SIGN': signature,
                'X-BAPI-TIMESTAMP': timestamp
            }
            
            response = requests.post(url, headers=headers, data=request_body_json)
            
            logger.info(f"[CANCEL-ALL-COMPLETE] Response Status: {response.status_code}")
            logger.debug(f"[CANCEL-ALL-COMPLETE] Response Body: {response.text}")
            
            if response.status_code == 200:
                response_json = response.json()
                if response_json.get("retCode") == 0:
                    result = response_json.get("result", {})
                    order_list = result.get("list", [])
                    success = result.get("success", "0")
                    
                    cancelled_count = len(order_list)
                    logger.info(f"[CANCEL-ALL-COMPLETE] ✅ {cancelled_count} Orders für {symbol} gecancelt (success: {success})")
                    
                    if cancelled_count > 0:
                        logger.info(f"[CANCEL-ALL-COMPLETE] 📋 Gecancelte Order-IDs:")
                        for order_item in order_list:
                            order_id = order_item.get("orderId", "N/A")
                            order_link_id = order_item.get("orderLinkId", "N/A")
                            logger.info(f"[CANCEL-ALL-COMPLETE]   • OrderId: {order_id}, OrderLinkId: {order_link_id}")
                    
                    # Kurze Verzögerung, damit die Orders vollständig gecancelt werden
                    time.sleep(0.5)
                    return True
                else:
                    ret_code = response_json.get("retCode")
                    ret_msg = response_json.get("retMsg", "Unknown error")
                    # Wenn keine Orders vorhanden waren, ist das auch OK
                    if ret_code == 10001:  # Invalid parameter oder keine Orders
                        logger.info(f"[CANCEL-ALL-COMPLETE] ℹ️ Keine Orders vorhanden für {symbol} (retCode: {ret_code})")
                        return True
                    logger.warning(f"[CANCEL-ALL-COMPLETE] ⚠️ Warnung beim Canceln aller Orders: {ret_msg} (retCode: {ret_code})")
                    return False
            else:
                logger.error(f"[CANCEL-ALL-COMPLETE] ❌ HTTP-Fehler beim Canceln aller Orders: {response.status_code}")
                return False
                
        except Exception as e:
            logger.error(f"[CANCEL-ALL-COMPLETE] ❌ Fehler beim Canceln aller Orders: {e}", exc_info=True)
            return False

    def cancel_sl_orders(self, symbol):
        """
        Storniert alle Stop-Loss-Orders für das angegebene Symbol.
        (DEPRECATED: Verwende stattdessen cancel_all_tp_sl_orders())
        
        :param symbol: Handelssymbol (z. B. "ETHUSDT")
        """
        try:
            # WICHTIG: Verwende fetch_open_orders_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
            open_orders = self.fetch_open_orders_direct(symbol, timeout=5)
            sl_orders = [order for order in open_orders if 
                        order['info'].get('stopOrderType') in ['StopLoss', 'PartialStopLoss'] or
                        (order['type'] in ['stop_market', 'Stop'] and 
                         order['info'].get('side') in ['Buy', 'Sell'])]

            if not sl_orders:
                logger.info(f"Keine SL Orders für {symbol} gefunden.")
                return

            logger.info(f"Gefundene SL Orders: {len(sl_orders)}")
            for order in sl_orders:
                try:
                    # WICHTIG: Verwende cancel_order_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
                    self.cancel_order_direct(order['id'], symbol, timeout=5)
                    logger.info(f"SL Order {order['id']} wurde storniert.")
                except Exception as e:
                    logger.error(f"Fehler beim Stornieren der SL Order {order['id']}: {e}", exc_info=True)

            logger.info(f"Alle SL Orders für {symbol} wurden storniert.")

        except Exception as e:
            logger.error(f"Fehler beim Abrufen der Orders: {e}", exc_info=True)

    # ----------------------------------------------------------------------
    # Zusätzliche Helfer für Burn-Logik
    # ----------------------------------------------------------------------

    def get_long_position(self, symbol):
        """
        Holt die aktuelle Long-Position (Size & Avg-Preis) für das Symbol.
        WICHTIG: Prüft positionIdx (1=Long, 0=OneWay) - wie bei Short/Dashboard, sonst werden
        Positionen fälschlich ausgeblendet wenn Bybit beide Positionen für ein Symbol liefert.
        """
        try:
            # WICHTIG: Verwende fetch_positions_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
            positions = self.fetch_positions_direct(symbol, timeout=5)
            for pos in positions:
                # fetch_positions_direct() gibt bereits CCXT-Format zurück (mit 'info')
                info = pos.get("info", {})
                pos_idx = int(info.get("positionIdx", 0) or 0)
                if info.get("side") == "Buy" and (pos_idx == 1 or pos_idx == 0):
                    size = float(info.get("size", 0) or 0)
                    if size <= 0:
                        continue
                    # Versuche zuerst avgPrice, fallback auf entryPrice
                    avg_price = info.get("avgPrice") or info.get("entryPrice")
                    avg_price = float(avg_price or 0)
                    # Spam-Reduktion: diese Zeile kann pro Tick mehrfach auftreten
                    logger.debug(f"Aktuelle Long-Position: Size={size}, AvgPrice={avg_price}")
                    return size, avg_price
            # Spam-Reduktion: häufige Polls sollen Logs nicht fluten
            logger.debug(f"Keine Long-Position für {symbol} gefunden.")
            return None, None
        except Exception as e:
            logger.error(f"Fehler beim Abrufen der Long-Position für {symbol}: {e}", exc_info=True)
            return None, None

    def get_current_price(self, symbol):
        """
        Holt den aktuellen Marktpreis über direkte Bybit-API.
        Verwendet Caching (30 Sekunden) und Retry-Logik mit exponential backoff bei Rate-Limit-Fehlern.
        """
        import time as time_module
        
        # Prüfe Cache zuerst
        current_time = time_module.time()
        if symbol in self._price_cache:
            cached_price, cache_time = self._price_cache[symbol]
            if current_time - cache_time < self._price_cache_timeout:
                logger.debug(f"Preis aus Cache für {symbol}: {cached_price}")
                return cached_price
        
        # Rate-Limit-Retry-Logik mit exponential backoff
        max_retries = 3
        base_delay = 2  # Start mit 2 Sekunden
        
        for attempt in range(max_retries):
            try:
                # WICHTIG: Verwende fetch_ticker_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
                ticker = self.fetch_ticker_direct(symbol, timeout=5)
                if ticker is None:
                    raise Exception("Ticker-Daten konnten nicht abgerufen werden")
                
                price = float(ticker.get("last") or ticker.get("close"))
                
                # Cache speichern
                self._price_cache[symbol] = (price, current_time)
                # Spam-Reduktion: get_current_price() wird sehr häufig gepollt.
                # Preis-Logs sind für Debug hilfreich, aber auf INFO-Level machen sie die Bot-Logs unlesbar.
                logger.debug(f"Aktueller Preis für {symbol}: {price}")
                return price
                
            except Exception as e:
                # Prüfe ob es ein Rate-Limit-Fehler ist
                error_str = str(e)
                error_type = str(type(e).__name__)
                is_rate_limit = (
                    "RateLimitExceeded" in error_type or
                    "10006" in error_str or
                    "Rate Limit" in error_str or
                    "Too many visits" in error_str or
                    "Exceeded the API Rate Limit" in error_str
                )
                
                if is_rate_limit:
                    if attempt < max_retries - 1:
                        delay = base_delay * (2 ** attempt)  # Exponential backoff: 2s, 4s, 8s
                        logger.warning(f"Rate-Limit für {symbol} (Versuch {attempt + 1}/{max_retries}) - warte {delay}s...")
                        time_module.sleep(delay)
                    else:
                        logger.error(f"Rate-Limit für {symbol} nach {max_retries} Versuchen überschritten. Verwende gecachten Preis falls verfügbar.")
                        # Verwende gecachten Preis als Fallback, auch wenn abgelaufen
                        if symbol in self._price_cache:
                            cached_price, _ = self._price_cache[symbol]
                            logger.warning(f"Verwende gecachten Preis für {symbol}: {cached_price}")
                            return cached_price
                        return None
                else:
                    # Andere Fehler: loggen und None zurückgeben
                    logger.error(f"Fehler beim Abrufen des Marktpreises für {symbol}: {e}", exc_info=True)
                    # Verwende gecachten Preis als Fallback
                    if symbol in self._price_cache:
                        cached_price, _ = self._price_cache[symbol]
                        logger.warning(f"Verwende gecachten Preis für {symbol} nach Fehler: {cached_price}")
                        return cached_price
                    return None
        
        return None

    def close_partial_long(self, symbol, size):
        """
        Schließt einen Teil der Long-Position als Market-Sell.
        Verwendet Bybit REST API direkt, um positionIdx: 1 für Hedge-Mode zu setzen.
        """
        try:
            # Hole Symbol-Info für korrekte Quantity-Rundung
            qty_step, min_qty = self.get_symbol_info(symbol)
            if qty_step:
                # Runde Size auf nächstes Vielfaches von qtyStep
                raw_size = float(size)
                rounded_size = round(raw_size / qty_step) * qty_step
                if rounded_size < min_qty:
                    rounded_size = min_qty
                # Stelle sicher, dass keine überflüssigen Dezimalstellen vorhanden sind
                decimals = len(str(qty_step).split('.')[-1]) if '.' in str(qty_step) else 0
                size = round(rounded_size, decimals)
            else:
                size = round(float(size), 2)
            
            logger.info(f"Schließe Teil-Long-Position: Symbol={symbol}, Size={size}")
            
            
            url = f'{self.base_url}/v5/order/create'
            recv_window = '20000'  # Erhöht für VPN-Latenz
            content_type = 'application/json'
            
            # Bybit Market-Order Request Body (für Partial Close)
            request_body = {
                "category": "linear",
                "symbol": symbol,
                "side": "Sell",
                "orderType": "Market",
                "qty": str(float(size)),
                "positionIdx": 1,  # 1 = Buy-Side (für Hedge-Mode)
                "timeInForce": "IOC",
                "reduceOnly": True  # Wichtig: reduceOnly=True für Partial Close
            }
            
            request_body_json = json.dumps(request_body)
            timestamp = str(int(time.time() * 1000))
            params_to_sign = timestamp + self.api_key + recv_window + request_body_json
            signature = hmac.new(
                self.secret_key.encode('utf-8'), params_to_sign.encode('utf-8'), hashlib.sha256
            ).hexdigest()
            
            headers = {
                'Content-Type': content_type,
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-RECV-WINDOW': recv_window,
                'X-BAPI-SIGN': signature,
                'X-BAPI-TIMESTAMP': timestamp
            }
            
            response = requests.post(url, headers=headers, data=request_body_json)
            
            if response.status_code == 200:
                response_json = response.json()
                if response_json.get("retCode") == 0:
                    order_id = response_json.get("result", {}).get("orderId", "N/A")
                    logger.info(f"Teil-Long-Order erfolgreich ausgeführt: OrderId={order_id}, Size={size}")
                    return response_json
                else:
                    logger.error(f"Fehler beim Schließen der Teil-Long-Position: {response_json.get('retMsg')} (retCode: {response_json.get('retCode')})")
                    return None
            else:
                logger.error(f"HTTP-Fehler beim Schließen der Teil-Long-Position: {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"Fehler beim Schließen der Teil-Long-Position: {e}", exc_info=True)
            return None

    def close_partial_short(self, symbol, size):
        """
        Schließt einen Teil der Short-Position (reduziert die Short-Size).
        Verwendet Bybit REST API direkt mit positionIdx=2 und reduceOnly=True.
        
        :param symbol: Handelssymbol (z. B. "SYMBOLUSDT")
        :param size: Anzahl Coins, die geschlossen werden sollen
        :return: Response von Bybit oder None bei Fehler
        """
        try:
            qty_step, min_qty = self.get_symbol_info(symbol)
            if qty_step:
                raw_size = float(size)
                rounded_size = round(raw_size / qty_step) * qty_step
                if rounded_size < min_qty:
                    rounded_size = min_qty
                decimals = len(str(qty_step).split('.')[-1]) if '.' in str(qty_step) else 0
                size = round(rounded_size, decimals)
            else:
                size = round(float(size), 2)

            logger.info(f"Schließe Teil-Short-Position: Symbol={symbol}, Size={size}")
            request_body = {
                "category": "linear",
                "symbol": symbol,
                "side": "Buy",  # Buy um Short-Position zu schließen
                "orderType": "Market",
                "qty": str(float(size)),
                "positionIdx": 2,  # 2 = Sell-Side (für Hedge-Mode Short)
                "reduceOnly": True
            }

            request_body_json = json.dumps(request_body)
            timestamp = str(int(time.time() * 1000))
            recv_window = '20000'  # Erhöht für VPN-Latenz
            params_to_sign = timestamp + self.api_key + recv_window + request_body_json
            signature = hmac.new(
                self.secret_key.encode('utf-8'), params_to_sign.encode('utf-8'), hashlib.sha256
            ).hexdigest()

            headers = {
                'Content-Type': 'application/json',
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-RECV-WINDOW': recv_window,
                'X-BAPI-SIGN': signature,
                'X-BAPI-TIMESTAMP': timestamp
            }

            url = f'{self.base_url}/v5/order/create'
            response = requests.post(url, headers=headers, data=request_body_json)
            
            if response.status_code == 200:
                response_json = response.json()
                if response_json.get("retCode") == 0:
                    order_id = response_json.get("result", {}).get("orderId", "N/A")
                    logger.info(f"Teil-Short-Order erfolgreich ausgeführt: OrderId={order_id}")
                    return response_json
                else:
                    logger.error(f"Fehler beim Schließen der Teil-Short-Position: {response_json.get('retMsg')} (retCode: {response_json.get('retCode')})")
                    return None
            else:
                logger.error(f"HTTP-Fehler beim Schließen der Teil-Short-Position: {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"Fehler beim Schließen der Teil-Short-Position: {e}", exc_info=True)
            return None

    def open_long_market(self, symbol, size):
        """
        Öffnet eine neue Long-Position als Market-Buy (für Rebuy oder Initial Opening).
        Verwendet Bybit REST API direkt, um positionIdx: 1 für Hedge-Mode zu setzen.
        """
        try:
            # Sicherheitsnetz: Stelle sicher, dass Hedge-Mode aktiv ist, bevor wir positionIdx=1 verwenden.
            if not self.ensure_hedge_mode(symbol, category="linear"):
                logger.error(f"[OPEN-LONG] ❌ Hedge-Mode konnte nicht gesetzt werden – abbrechen für {symbol}")
                return None

            # Nach Hedge-Mode: maximalen Leverage für das Symbol setzen.
            if not self.ensure_max_leverage(symbol, category="linear"):
                logger.error(f"[OPEN-LONG] ❌ Max-Leverage konnte nicht gesetzt werden – abbrechen für {symbol}")
                return None
            # Hole Symbol-Info für korrekte Quantity-Rundung
            qty_step, min_qty = self.get_symbol_info(symbol)
            if qty_step:
                # Runde Size auf nächstes Vielfaches von qtyStep
                raw_size = float(size)
                rounded_size = round(raw_size / qty_step) * qty_step
                if rounded_size < min_qty:
                    rounded_size = min_qty
                # Stelle sicher, dass keine überflüssigen Dezimalstellen vorhanden sind
                decimals = len(str(qty_step).split('.')[-1]) if '.' in str(qty_step) else 0
                size = round(rounded_size, decimals)
            else:
                size = round(float(size), 2)
            
            logger.info(f"Öffne Long-Position: Symbol={symbol}, Size={size}")
            
            url = f'{self.base_url}/v5/order/create'
            recv_window = '20000'  # Erhöht für VPN-Latenz
            content_type = 'application/json'
            
            # Bybit Market-Order Request Body (für Long-Position)
            request_body = {
                "category": "linear",
                "symbol": symbol,
                "side": "Buy",
                "orderType": "Market",
                "qty": str(float(size)),
                "positionIdx": 1,  # 1 = Buy-Side (für Hedge-Mode)
                "timeInForce": "IOC"
            }
            
            request_body_json = json.dumps(request_body)
            timestamp = str(int(time.time() * 1000))
            params_to_sign = timestamp + self.api_key + recv_window + request_body_json
            signature = hmac.new(
                self.secret_key.encode('utf-8'),
                params_to_sign.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            headers = {
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-SIGN': signature,
                'X-BAPI-SIGN-TYPE': '2',
                'X-BAPI-TIMESTAMP': timestamp,
                'X-BAPI-RECV-WINDOW': recv_window,
                'Content-Type': content_type
            }
            
            response = requests.post(url, headers=headers, data=request_body_json)
            response.raise_for_status()
            result = response.json()
            
            if result.get('retCode') == 0:
                order_info = result.get('result', {})
                logger.info(f"Long-Order ausgeführt: {order_info}")
                return order_info
            else:
                error_msg = f"Bybit API Fehler: {result.get('retMsg', 'Unbekannter Fehler')}"
                logger.error(error_msg)
                return None
                
        except Exception as e:
            logger.error(f"Fehler beim Öffnen der Long-Position: {e}", exc_info=True)
            return None

    def open_long_limit(self, symbol, price, size, order_link_id=None):
        """Öffnet eine neue Long-Limit-Order im Hedge-Mode."""
        try:
            if not self.ensure_hedge_mode(symbol, category="linear"):
                logger.error(f"[OPEN-LONG-LIMIT] ❌ Hedge-Mode konnte nicht gesetzt werden – abbrechen für {symbol}")
                return None
            if not self.ensure_max_leverage(symbol, category="linear"):
                logger.error(f"[OPEN-LONG-LIMIT] ❌ Max-Leverage konnte nicht gesetzt werden – abbrechen für {symbol}")
                return None

            qty_step, min_qty = self.get_symbol_info(symbol)
            tick_size = self.get_symbol_tick_size(symbol)
            raw_size = float(size)
            raw_price = float(price)
            if qty_step:
                rounded_size = round(raw_size / qty_step) * qty_step
                if rounded_size < min_qty:
                    rounded_size = min_qty
                decimals = len(str(qty_step).split('.')[-1]) if '.' in str(qty_step) else 0
                size = round(rounded_size, decimals)
            else:
                size = round(raw_size, 2)
            if tick_size:
                price_decimals = len(str(tick_size).split('.')[-1]) if '.' in str(tick_size) else 0
                price = round(round(raw_price / tick_size) * tick_size, price_decimals)
            else:
                price = round(raw_price, 6)

            request_body = {
                "category": "linear",
                "symbol": symbol,
                "side": "Buy",
                "orderType": "Limit",
                "qty": str(float(size)),
                "price": str(float(price)),
                "positionIdx": 1,
                "timeInForce": "GTC",
                "reduceOnly": False,
            }
            if order_link_id:
                request_body["orderLinkId"] = str(order_link_id)

            request_body_json = json.dumps(request_body)
            timestamp = str(int(time.time() * 1000))
            recv_window = '20000'
            params_to_sign = timestamp + self.api_key + recv_window + request_body_json
            signature = hmac.new(self.secret_key.encode('utf-8'), params_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()
            headers = {
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-SIGN': signature,
                'X-BAPI-SIGN-TYPE': '2',
                'X-BAPI-TIMESTAMP': timestamp,
                'X-BAPI-RECV-WINDOW': recv_window,
                'Content-Type': 'application/json'
            }
            response = requests.post(f'{self.base_url}/v5/order/create', headers=headers, data=request_body_json)
            if response.status_code != 200:
                logger.error(f"[OPEN-LONG-LIMIT] HTTP-Fehler {response.status_code}: {response.text[:300]}")
                return None
            result = response.json()
            if result.get('retCode') == 0:
                order_info = result.get('result', {})
                logger.info(f"[OPEN-LONG-LIMIT] ✅ Long-Limit gesetzt: {order_info}")
                return order_info
            logger.error(f"[OPEN-LONG-LIMIT] Bybit API Fehler: {result.get('retMsg', 'Unbekannter Fehler')}")
            return None
        except Exception as e:
            logger.error(f"[OPEN-LONG-LIMIT] Fehler: {e}", exc_info=True)
            return None

    def close_long_limit(self, symbol, price, size, order_link_id=None):
        """Setzt eine reduceOnly Limit-Order zum Schließen eines Long-Teils."""
        try:
            qty_step, min_qty = self.get_symbol_info(symbol)
            tick_size = self.get_symbol_tick_size(symbol)
            raw_size = float(size)
            raw_price = float(price)
            if qty_step:
                rounded_size = round(raw_size / qty_step) * qty_step
                if rounded_size < min_qty:
                    rounded_size = min_qty
                decimals = len(str(qty_step).split('.')[-1]) if '.' in str(qty_step) else 0
                size = round(rounded_size, decimals)
            else:
                size = round(raw_size, 2)
            if tick_size:
                price_decimals = len(str(tick_size).split('.')[-1]) if '.' in str(tick_size) else 0
                price = round(round(raw_price / tick_size) * tick_size, price_decimals)
            else:
                price = round(raw_price, 6)

            request_body = {
                "category": "linear",
                "symbol": symbol,
                "side": "Sell",
                "orderType": "Limit",
                "qty": str(float(size)),
                "price": str(float(price)),
                "positionIdx": 1,
                "timeInForce": "GTC",
                "reduceOnly": True,
                "closeOnTrigger": False,
            }
            if order_link_id:
                request_body["orderLinkId"] = str(order_link_id)

            request_body_json = json.dumps(request_body)
            timestamp = str(int(time.time() * 1000))
            recv_window = '20000'
            params_to_sign = timestamp + self.api_key + recv_window + request_body_json
            signature = hmac.new(self.secret_key.encode('utf-8'), params_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()
            headers = {
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-SIGN': signature,
                'X-BAPI-SIGN-TYPE': '2',
                'X-BAPI-TIMESTAMP': timestamp,
                'X-BAPI-RECV-WINDOW': recv_window,
                'Content-Type': 'application/json'
            }
            response = requests.post(f'{self.base_url}/v5/order/create', headers=headers, data=request_body_json)
            if response.status_code != 200:
                logger.error(f"[CLOSE-LONG-LIMIT] HTTP-Fehler {response.status_code}: {response.text[:300]}")
                return None
            result = response.json()
            if result.get('retCode') == 0:
                order_info = result.get('result', {})
                logger.info(f"[CLOSE-LONG-LIMIT] ✅ Long-TP-Limit gesetzt: {order_info}")
                return order_info
            logger.error(f"[CLOSE-LONG-LIMIT] Bybit API Fehler: {result.get('retMsg', 'Unbekannter Fehler')}")
            return None
        except Exception as e:
            logger.error(f"[CLOSE-LONG-LIMIT] Fehler: {e}", exc_info=True)
            return None

    def close_long_market(self, symbol, size):
        """
        Reduziert eine Long-Position per Market-Sell (Burn).
        """
        try:
            qty_step, min_qty = self.get_symbol_info(symbol)
            raw_size = float(size)
            if qty_step:
                rounded_size = round(raw_size / qty_step) * qty_step
                # WICHTIG: Wenn rounded_size zu 0.0 wird (weil raw_size < qty_step), verwende raw_size direkt
                # ABER: Nur wenn raw_size >= min_qty, sonst verwende min_qty
                if rounded_size == 0.0 and raw_size > 0:
                    if min_qty and raw_size >= min_qty:
                        rounded_size = raw_size
                    elif min_qty:
                        rounded_size = min_qty
                    else:
                        rounded_size = raw_size
                elif rounded_size < min_qty:
                    rounded_size = min_qty
                decimals = len(str(qty_step).split('.')[-1]) if '.' in str(qty_step) else 0
                size = round(rounded_size, decimals)
            else:
                size = round(raw_size, 2)
            logger.info(f"Market Burn: Reduce Long position – Symbol={symbol}, Size={size} (raw={raw_size:.6f}, qty_step={qty_step if qty_step else 'N/A'}, min_qty={min_qty if min_qty else 'N/A'})")
            url = f'{self.base_url}/v5/order/create'
            recv_window = '20000'
            content_type = 'application/json'
            request_body = {
                "category": "linear",
                "symbol": symbol,
                "side": "Sell",
                "orderType": "Market",
                "qty": str(float(size)),
                "positionIdx": 1,
                "reduceOnly": True,
                "timeInForce": "IOC"
            }
            request_body_json = json.dumps(request_body)
            timestamp = str(int(time.time() * 1000))
            params_to_sign = timestamp + self.api_key + recv_window + request_body_json
            signature = hmac.new(self.secret_key.encode('utf-8'), params_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()
            headers = {
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-SIGN': signature,
                'X-BAPI-SIGN-TYPE': '2',
                'X-BAPI-TIMESTAMP': timestamp,
                'X-BAPI-RECV-WINDOW': recv_window,
                'Content-Type': content_type
            }
            response = requests.post(url, headers=headers, data=request_body_json)
            if response.status_code != 200:
                logger.error(f"[BURN] HTTP-Fehler bei Market-Burn: {response.status_code}")
                return None
            result = response.json()
            if result.get('retCode') == 0:
                order_info = result.get('result', {})
                logger.info(f"✅ Market-Burn Long erfolgreich: {order_info}")
                return order_info
            else:
                logger.error(f"❌ Market-Burn fehlgeschlagen: {result.get('retMsg')} (retCode={result.get('retCode')})")
                return None
        except Exception as e:
            logger.error(f"❌ Fehler beim Market-Burn: {e}", exc_info=True)
            return None

    def close_short_market(self, symbol, size):
        """
        Reduziert eine Short-Position per Market-Buy (Burn).
        GESPIEGELT: Wie close_long_market(), nur für Short-Positionen.
        """
        try:
            qty_step, min_qty = self.get_symbol_info(symbol)
            raw_size = float(size)
            if qty_step:
                rounded_size = round(raw_size / qty_step) * qty_step
                # WICHTIG: Wenn rounded_size zu 0.0 wird (weil raw_size < qty_step), verwende raw_size direkt
                # ABER: Nur wenn raw_size >= min_qty, sonst verwende min_qty
                if rounded_size == 0.0 and raw_size > 0:
                    if min_qty and raw_size >= min_qty:
                        rounded_size = raw_size
                    elif min_qty:
                        rounded_size = min_qty
                    else:
                        rounded_size = raw_size
                elif rounded_size < min_qty:
                    rounded_size = min_qty
                decimals = len(str(qty_step).split('.')[-1]) if '.' in str(qty_step) else 0
                size = round(rounded_size, decimals)
            else:
                size = round(raw_size, 2)
            logger.info(f"Market Burn: Reduce Short position – Symbol={symbol}, Size={size} (raw={raw_size:.6f}, qty_step={qty_step if qty_step else 'N/A'}, min_qty={min_qty if min_qty else 'N/A'})")
            url = f'{self.base_url}/v5/order/create'
            recv_window = '20000'
            content_type = 'application/json'
            request_body = {
                "category": "linear",
                "symbol": symbol,
                "side": "Buy",  # Buy um Short-Position zu schließen
                "orderType": "Market",
                "qty": str(float(size)),
                "positionIdx": 2,  # 2 = Sell-Side (für Hedge-Mode Short)
                "reduceOnly": True,
                "timeInForce": "IOC"
            }
            request_body_json = json.dumps(request_body)
            timestamp = str(int(time.time() * 1000))
            params_to_sign = timestamp + self.api_key + recv_window + request_body_json
            signature = hmac.new(self.secret_key.encode('utf-8'), params_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()
            headers = {
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-SIGN': signature,
                'X-BAPI-SIGN-TYPE': '2',
                'X-BAPI-TIMESTAMP': timestamp,
                'X-BAPI-RECV-WINDOW': recv_window,
                'Content-Type': content_type
            }
            response = requests.post(url, headers=headers, data=request_body_json)
            if response.status_code != 200:
                logger.error(f"[BURN] HTTP-Fehler bei Market-Burn: {response.status_code}")
                return None
            result = response.json()
            if result.get('retCode') == 0:
                order_info = result.get('result', {})
                logger.info(f"✅ Market-Burn Short erfolgreich: {order_info}")
                return order_info
            else:
                logger.error(f"❌ Market-Burn fehlgeschlagen: {result.get('retMsg')} (retCode={result.get('retCode')})")
                return None
        except Exception as e:
            logger.error(f"❌ Fehler beim Market-Burn: {e}", exc_info=True)
            return None

    def open_short_market(self, symbol, size):
        """
        Öffnet eine neue Short-Position als Market-Sell (für Rebuy oder Initial Opening).
        Verwendet Bybit REST API direkt mit positionIdx=2 für Hedge-Mode.
        """
        try:
            # Sicherheitsnetz: Stelle sicher, dass Hedge-Mode aktiv ist, bevor wir positionIdx=2 verwenden.
            if not self.ensure_hedge_mode(symbol, category="linear"):
                logger.error(f"[OPEN-SHORT] ❌ Hedge-Mode konnte nicht gesetzt werden – abbrechen für {symbol}")
                return None

            # Nach Hedge-Mode: maximalen Leverage für das Symbol setzen.
            if not self.ensure_max_leverage(symbol, category="linear"):
                logger.error(f"[OPEN-SHORT] ❌ Max-Leverage konnte nicht gesetzt werden – abbrechen für {symbol}")
                return None
            # Hole Symbol-Info für korrekte Quantity-Rundung
            qty_step, min_qty = self.get_symbol_info(symbol)
            if qty_step:
                # Runde Size auf nächstes Vielfaches von qtyStep
                raw_size = float(size)
                rounded_size = round(raw_size / qty_step) * qty_step
                if rounded_size < min_qty:
                    rounded_size = min_qty
                # Stelle sicher, dass keine überflüssigen Dezimalstellen vorhanden sind
                decimals = len(str(qty_step).split('.')[-1]) if '.' in str(qty_step) else 0
                size = round(rounded_size, decimals)
            else:
                size = round(float(size), 2)
            
            logger.info(f"Öffne Short-Position: Symbol={symbol}, Size={size}")
            
            url = f'{self.base_url}/v5/order/create'
            recv_window = '20000'  # Erhöht für VPN-Latenz
            content_type = 'application/json'
            
            # Bybit Market-Order Request Body (für Short-Position)
            request_body = {
                "category": "linear",
                "symbol": symbol,
                "side": "Sell",
                "orderType": "Market",
                "qty": str(float(size)),
                "positionIdx": 2,  # 2 = Sell-Side (für Hedge-Mode Short)
                "timeInForce": "IOC"
            }
            
            request_body_json = json.dumps(request_body)
            timestamp = str(int(time.time() * 1000))
            params_to_sign = timestamp + self.api_key + recv_window + request_body_json
            signature = hmac.new(
                self.secret_key.encode('utf-8'),
                params_to_sign.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            headers = {
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-SIGN': signature,
                'X-BAPI-SIGN-TYPE': '2',
                'X-BAPI-TIMESTAMP': timestamp,
                'X-BAPI-RECV-WINDOW': recv_window,
                'Content-Type': content_type
            }
            
            response = requests.post(url, headers=headers, data=request_body_json)
            
            # Log Response für besseres Debugging
            logger.debug(f"Short-Market-Order Response Status: {response.status_code}")
            logger.debug(f"Short-Market-Order Request Body: {request_body_json}")
            
            # Prüfe HTTP Status
            if response.status_code != 200:
                error_msg = f"HTTP-Fehler {response.status_code}: {response.text[:500]}"
                logger.error(f"❌ {error_msg}")
                logger.error(f"   Request Body: {request_body_json}")
                return None
            
            result = response.json()
            logger.debug(f"Short-Market-Order API Response: {result}")
            
            if result.get('retCode') == 0:
                order_info = result.get('result', {})
                logger.info(f"✅ Short-Order erfolgreich erstellt: {order_info}")
                # Prüfe ob Order wirklich gefüllt wurde
                order_id = order_info.get('orderId', 'N/A')
                order_status = order_info.get('orderStatus', 'N/A')
                logger.info(f"   Order-ID: {order_id}, Status: {order_status}")
                
                # WICHTIG: Market Orders sollten sofort gefüllt werden
                if order_status not in ['Filled', 'PartiallyFilled']:
                    logger.info(f"ℹ️ Short-Order Status ist nicht 'Filled': {order_status}")
                    logger.info("ℹ️ Order könnte noch nicht gefüllt sein - prüfe später erneut")
                    # Für Market Orders sollte der Status normalerweise 'Filled' sein
                    # Wenn nicht, könnte es ein Problem geben (z.B. unzureichendes Margin)
                    # Aber wir geben die Order-Info trotzdem zurück, damit der Caller prüfen kann
                
                return order_info
            else:
                ret_code = result.get('retCode', 'N/A')
                ret_msg = result.get('retMsg', 'Unbekannter Fehler')
                error_msg = f"Bybit API Fehler (retCode={ret_code}): {ret_msg}"
                logger.error(f"❌ {error_msg}")
                logger.error(f"   Symbol: {symbol}, Size: {size}")
                logger.error(f"   Request Body: {request_body_json}")
                logger.error(f"   Full Response: {result}")
                return None
                
        except requests.exceptions.HTTPError as e:
            error_msg = f"HTTP-Fehler beim Öffnen der Short-Position: {e}"
            logger.error(f"❌ {error_msg}")
            logger.error(f"   Response Text: {e.response.text[:500] if hasattr(e, 'response') else 'N/A'}")
            logger.error(f"   Symbol: {symbol}, Size: {size}")
            return None
        except Exception as e:
            error_msg = f"Unerwarteter Fehler beim Öffnen der Short-Position: {e}"
            logger.error(f"❌ {error_msg}", exc_info=True)
            logger.error(f"   Symbol: {symbol}, Size: {size}")
            return None

    def open_short_limit(self, symbol, price, size, order_link_id=None):
        """Öffnet eine neue Short-Limit-Order im Hedge-Mode."""
        try:
            if not self.ensure_hedge_mode(symbol, category="linear"):
                logger.error(f"[OPEN-SHORT-LIMIT] ❌ Hedge-Mode konnte nicht gesetzt werden – abbrechen für {symbol}")
                return None
            if not self.ensure_max_leverage(symbol, category="linear"):
                logger.error(f"[OPEN-SHORT-LIMIT] ❌ Max-Leverage konnte nicht gesetzt werden – abbrechen für {symbol}")
                return None

            qty_step, min_qty = self.get_symbol_info(symbol)
            tick_size = self.get_symbol_tick_size(symbol)
            raw_size = float(size)
            raw_price = float(price)
            if qty_step:
                rounded_size = round(raw_size / qty_step) * qty_step
                if rounded_size < min_qty:
                    rounded_size = min_qty
                decimals = len(str(qty_step).split('.')[-1]) if '.' in str(qty_step) else 0
                size = round(rounded_size, decimals)
            else:
                size = round(raw_size, 2)
            if tick_size:
                price_decimals = len(str(tick_size).split('.')[-1]) if '.' in str(tick_size) else 0
                price = round(round(raw_price / tick_size) * tick_size, price_decimals)
            else:
                price = round(raw_price, 6)

            request_body = {
                "category": "linear",
                "symbol": symbol,
                "side": "Sell",
                "orderType": "Limit",
                "qty": str(float(size)),
                "price": str(float(price)),
                "positionIdx": 2,
                "timeInForce": "GTC",
                "reduceOnly": False,
            }
            if order_link_id:
                request_body["orderLinkId"] = str(order_link_id)

            request_body_json = json.dumps(request_body)
            timestamp = str(int(time.time() * 1000))
            recv_window = '20000'
            params_to_sign = timestamp + self.api_key + recv_window + request_body_json
            signature = hmac.new(self.secret_key.encode('utf-8'), params_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()
            headers = {
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-SIGN': signature,
                'X-BAPI-SIGN-TYPE': '2',
                'X-BAPI-TIMESTAMP': timestamp,
                'X-BAPI-RECV-WINDOW': recv_window,
                'Content-Type': 'application/json'
            }
            response = requests.post(f'{self.base_url}/v5/order/create', headers=headers, data=request_body_json)
            if response.status_code != 200:
                logger.error(f"[OPEN-SHORT-LIMIT] HTTP-Fehler {response.status_code}: {response.text[:300]}")
                return None
            result = response.json()
            if result.get('retCode') == 0:
                order_info = result.get('result', {})
                logger.info(f"[OPEN-SHORT-LIMIT] ✅ Short-Limit gesetzt: {order_info}")
                return order_info
            logger.error(f"[OPEN-SHORT-LIMIT] Bybit API Fehler: {result.get('retMsg', 'Unbekannter Fehler')}")
            return None
        except Exception as e:
            logger.error(f"[OPEN-SHORT-LIMIT] Fehler: {e}", exc_info=True)
            return None

    def close_short_limit(self, symbol, price, size, order_link_id=None):
        """Setzt eine reduceOnly Limit-Order zum Schließen eines Short-Teils."""
        try:
            qty_step, min_qty = self.get_symbol_info(symbol)
            tick_size = self.get_symbol_tick_size(symbol)
            raw_size = float(size)
            raw_price = float(price)
            if qty_step:
                rounded_size = round(raw_size / qty_step) * qty_step
                if rounded_size < min_qty:
                    rounded_size = min_qty
                decimals = len(str(qty_step).split('.')[-1]) if '.' in str(qty_step) else 0
                size = round(rounded_size, decimals)
            else:
                size = round(raw_size, 2)
            if tick_size:
                price_decimals = len(str(tick_size).split('.')[-1]) if '.' in str(tick_size) else 0
                price = round(round(raw_price / tick_size) * tick_size, price_decimals)
            else:
                price = round(raw_price, 6)

            request_body = {
                "category": "linear",
                "symbol": symbol,
                "side": "Buy",
                "orderType": "Limit",
                "qty": str(float(size)),
                "price": str(float(price)),
                "positionIdx": 2,
                "timeInForce": "GTC",
                "reduceOnly": True,
                "closeOnTrigger": False,
            }
            if order_link_id:
                request_body["orderLinkId"] = str(order_link_id)

            request_body_json = json.dumps(request_body)
            timestamp = str(int(time.time() * 1000))
            recv_window = '20000'
            params_to_sign = timestamp + self.api_key + recv_window + request_body_json
            signature = hmac.new(self.secret_key.encode('utf-8'), params_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()
            headers = {
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-SIGN': signature,
                'X-BAPI-SIGN-TYPE': '2',
                'X-BAPI-TIMESTAMP': timestamp,
                'X-BAPI-RECV-WINDOW': recv_window,
                'Content-Type': 'application/json'
            }
            response = requests.post(f'{self.base_url}/v5/order/create', headers=headers, data=request_body_json)
            if response.status_code != 200:
                logger.error(f"[CLOSE-SHORT-LIMIT] HTTP-Fehler {response.status_code}: {response.text[:300]}")
                return None
            result = response.json()
            if result.get('retCode') == 0:
                order_info = result.get('result', {})
                logger.info(f"[CLOSE-SHORT-LIMIT] ✅ Short-TP-Limit gesetzt: {order_info}")
                return order_info
            logger.error(f"[CLOSE-SHORT-LIMIT] Bybit API Fehler: {result.get('retMsg', 'Unbekannter Fehler')}")
            return None
        except Exception as e:
            logger.error(f"[CLOSE-SHORT-LIMIT] Fehler: {e}", exc_info=True)
            return None

    def get_short_position(self, symbol):
        """
        Holt die aktuelle Short-Position (Size & Avg-Preis) für das Symbol.
        WICHTIG: Prüft positionIdx (2=Short, 0=OneWay) - spiegelgleich zu get_long_position,
        sonst werden Short-Positionen fälschlich ausgeblendet wenn Bybit beide Positionen liefert.
        """
        try:
            # WICHTIG: Verwende fetch_positions_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
            positions = self.fetch_positions_direct(symbol, timeout=5)
            for pos in positions:
                # fetch_positions_direct() gibt bereits CCXT-Format zurück (mit 'info')
                info = pos.get("info", {})
                pos_idx = int(info.get("positionIdx", 0) or 0)
                if info.get("side") == "Sell" and (pos_idx == 2 or pos_idx == 0):
                    size = float(info.get("size", 0) or 0)
                    if size <= 0:
                        continue
                    # Versuche zuerst avgPrice, fallback auf entryPrice
                    avg_price = info.get("avgPrice") or info.get("entryPrice")
                    avg_price = float(avg_price or 0)
                    # Spam-Reduktion: diese Zeile kann pro Tick mehrfach auftreten
                    logger.debug(f"Aktuelle Short-Position: Size={size}, AvgPrice={avg_price}")
                    return size, avg_price
            # Spam-Reduktion: häufige Polls sollen Logs nicht fluten
            logger.debug(f"Keine Short-Position für {symbol} gefunden.")
            return None, None
        except Exception as e:
            logger.error(f"Fehler beim Abrufen der Short-Position für {symbol}: {e}", exc_info=True)
            return None, None

    def get_breakeven_price(self, symbol, side=None):
        """
        Ruft den Break-Even-Preis einer Position von Bybit ab.
        
        Dokumentation: https://bybit-exchange.github.io/docs/v5/position/list
        
        :param symbol: Handelssymbol (z.B. "ETHUSDT")
        :param side: Optional - "Buy" für Long, "Sell" für Short. Wenn None, wird die erste gefundene Position zurückgegeben.
        :return: Tuple (break_even_price, side) oder (None, None) wenn keine Position gefunden
        """
        try:
            positions = self.fetch_positions_direct(symbol, timeout=5)
            
            for pos in positions:
                info = pos.get("info", {})
                pos_side = info.get("side", "")
                size = float(info.get("size", 0) or 0)
                
                # WICHTIG: Überspringe leere Positionen (Size = 0)
                # Bybit gibt auch Positionen mit Size 0 zurück, wenn das Symbol bereits gehandelt wurde
                if size <= 0:
                    continue
                
                # Filtere nach Side, falls angegeben
                if side and pos_side != side:
                    continue
                
                # Break-Even-Preis abrufen
                break_even_price = info.get("breakEvenPrice")
                
                if break_even_price:
                    be_price = float(break_even_price)
                    # Prüfe ob BE-Preis gültig ist (> 0)
                    if be_price > 0:
                        logger.info(f"Break-Even-Preis für {symbol} ({pos_side}): {be_price}")
                        return be_price, pos_side
                    else:
                        logger.warning(f"Break-Even-Preis ist 0 für {symbol} ({pos_side}) - ungültig")
                else:
                    logger.warning(f"Break-Even-Preis nicht verfügbar für {symbol} ({pos_side})")
            
            logger.info(f"Keine offene Position mit Break-Even-Preis für {symbol} gefunden.")
            return None, None
            
        except Exception as e:
            logger.error(f"Fehler beim Abrufen des Break-Even-Preises für {symbol}: {e}", exc_info=True)
            return None, None

    def calculate_be_plus_percent(self, break_even_price, percent=1.0, side=None):
        """
        Berechnet Break-Even-Preis + Prozent (Richtung abhängig von Position-Side).
        
        WICHTIG: Bei Short-Positionen (Sell) bedeutet Profit einen FALLENDEN Preis!
        - Long (Buy): BE + 1% = BE * 1.01 (Preis steigt)
        - Short (Sell): BE + 1% = BE * 0.99 (Preis fällt für Profit)
        
        :param break_even_price: Break-Even-Preis
        :param percent: Prozent (Standard: 1.0 für 1%)
        :param side: Optional - "Buy" für Long, "Sell" für Short. Wenn None, wird + verwendet.
        :return: BE + Prozent (oder BE - Prozent bei Short) oder None bei ungültigem Input
        """
        if break_even_price is None or break_even_price <= 0:
            return None
        
        # Bei Short-Positionen (Sell): Profit bedeutet fallender Preis → BE - Prozent
        if side == "Sell":
            multiplier = 1 - (percent / 100.0)
        else:
            # Bei Long-Positionen (Buy) oder wenn side nicht angegeben: BE + Prozent
            multiplier = 1 + (percent / 100.0)
        
        return float(break_even_price) * multiplier

    def get_be_plus_percent_price(self, symbol, side=None, percent=1.0):
        """
        Ruft Break-Even-Preis ab und berechnet BE + Prozent (Richtung abhängig von Position-Side).
        Kombiniert get_breakeven_price() und calculate_be_plus_percent().
        
        WICHTIG: Bei Short-Positionen (Sell) bedeutet Profit einen FALLENDEN Preis!
        - Long (Buy): BE + 1% = BE * 1.01 (Preis steigt)
        - Short (Sell): BE + 1% = BE * 0.99 (Preis fällt für Profit)
        
        :param symbol: Handelssymbol (z.B. "ETHUSDT")
        :param side: Optional - "Buy" für Long, "Sell" für Short
        :param percent: Prozent (Standard: 1.0 für 1%)
        :return: Tuple (be_plus_percent_price, break_even_price, side) oder (None, None, None) bei Fehler
        """
        break_even_price, pos_side = self.get_breakeven_price(symbol, side)
        
        if break_even_price is None:
            return None, None, None
        
        # Verwende die tatsächliche Position-Side für die Berechnung
        be_plus_price = self.calculate_be_plus_percent(break_even_price, percent, pos_side)
        
        if be_plus_price:
            if pos_side == "Sell":
                logger.info(f"BE - {percent}% für {symbol} ({pos_side}): {be_plus_price:.6f} (BE: {break_even_price:.6f})")
            else:
                logger.info(f"BE + {percent}% für {symbol} ({pos_side}): {be_plus_price:.6f} (BE: {break_even_price:.6f})")
        
        return be_plus_price, break_even_price, pos_side

    def cancel_order_direct(self, order_id, symbol, order_link_id=None, timeout=5):
        """
        Cancelt eine einzelne Order direkt über Bybit API v5 mit explizitem Timeout.
        Dies ist schneller und zuverlässiger als CCXT.
        
        Dokumentation: https://bybit-exchange.github.io/docs/v5/order/cancel
        
        :param order_id: Order ID (entweder orderId oder orderLinkId erforderlich)
        :param symbol: Handelssymbol (z.B. "XPINUSDT")
        :param order_link_id: Optional - User customised order ID
        :param timeout: Timeout in Sekunden (Standard: 5)
        :return: dict mit orderId und orderLinkId oder None bei Fehler
        """
        url = f'{self.base_url}/v5/order/cancel'
        recv_window = '20000'
        
        try:
            import time as time_module
            start_time = time_module.time()
            
            # Request Body (POST)
            body = {
                'category': 'linear',
                'symbol': symbol,
            }
            
            # Entweder orderId oder orderLinkId muss gesetzt sein
            if order_id:
                body['orderId'] = str(order_id)
            elif order_link_id:
                body['orderLinkId'] = str(order_link_id)
            else:
                logger.error(f"[CANCEL-ORDER-DIRECT] ❌ Weder orderId noch orderLinkId angegeben")
                return None
            
            # Erstelle Signatur (POST-Request: timestamp + api_key + recv_window + body_json)
            # WICHTIG: Verwende json.dumps() OHNE separators, um konsistent mit anderen erfolgreichen Funktionen zu sein
            timestamp = str(int(time.time() * 1000))
            body_json = json.dumps(body)  # Ohne separators, wie in set_tp_short_order
            signature_payload = f"{timestamp}{self.api_key}{recv_window}{body_json}".encode('utf-8')
            signature = hmac.new(
                self.secret_key.encode('utf-8'),
                signature_payload,
                hashlib.sha256
            ).hexdigest()
            
            # Header
            headers = {
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-TIMESTAMP': timestamp,
                'X-BAPI-RECV-WINDOW': recv_window,
                'X-BAPI-SIGN': signature,
                'Content-Type': 'application/json',
            }
            
            # WICHTIG: Verwende data=body_json statt json=body, damit die Signatur mit dem tatsächlich gesendeten Body übereinstimmt
            # (requests.post mit json= serialisiert das Dict selbst, was zu einer anderen Signatur führen kann)
            response = requests.post(url, headers=headers, data=body_json, timeout=timeout)
            
            elapsed = time_module.time() - start_time
            logger.debug(f"[CANCEL-ORDER-DIRECT] Request abgeschlossen nach {elapsed:.2f}s")
            
            if response.status_code == 200:
                response_json = response.json()
                ret_code = response_json.get("retCode")
                ret_msg = response_json.get("retMsg", "Unknown error")
                
                if ret_code == 0:
                    # ✅ NUR retCode == 0 bedeutet erfolgreiches Cancel
                    result = response_json.get("result", {})
                    cancelled_order_id = result.get("orderId", order_id)
                    logger.info(f"[CANCEL-ORDER-DIRECT] ✅ Order {cancelled_order_id} erfolgreich gecancelt")
                    return result
                else:
                    # ❌ BUG 4 FIX: Nur bestimmte "harmlose" Fehler werden als OK behandelt
                    # Alle anderen Fehler (inkl. retCode 10004 = Error sign) werden als Fehler behandelt
                    if ret_code == 110001 or "order not exists" in ret_msg.lower() or "too late to cancel" in ret_msg.lower():
                        # Order existiert nicht mehr (bereits gefüllt/gecancelt) - das ist OK
                        logger.debug(f"[CANCEL-ORDER-DIRECT] ℹ️ Order {order_id} existiert nicht mehr (bereits gefüllt/gecancelt) - OK")
                        return {"orderId": order_id, "orderLinkId": ""}  # Return success für bereits gecancelte Orders
                    else:
                        # ❌ KRITISCH: retCode != 0 und nicht "harmlos" → Cancel fehlgeschlagen
                        # Beispiel: retCode 10004 = Error sign → Order könnte noch existieren!
                        logger.error(f"[CANCEL-ORDER-DIRECT] ❌ Bybit API Fehler (retCode={ret_code}): {ret_msg}")
                        logger.error(f"[CANCEL-ORDER-DIRECT] ⚠️ Order {order_id} wurde NICHT gecancelt - könnte noch aktiv sein!")
                        return None
            else:
                logger.error(f"[CANCEL-ORDER-DIRECT] HTTP-Fehler: {response.status_code} - {response.text[:200]}")
                return None
                
        except requests.exceptions.Timeout:
            logger.error(f"[CANCEL-ORDER-DIRECT] ⏱️ Timeout nach {timeout}s")
            return None
        except Exception as e:
            logger.error(f"[CANCEL-ORDER-DIRECT] ❌ Fehler beim Canceln der Order {order_id}: {e}", exc_info=True)
            return None

    def cancel_all_orders_direct(self, symbol=None, base_coin=None, settle_coin=None, order_filter=None, timeout=5):
        """
        Cancelt alle Orders direkt über Bybit API v5 mit explizitem Timeout.
        Dies ist schneller und zuverlässiger als CCXT.
        
        Dokumentation: https://bybit-exchange.github.io/docs/v5/order/cancel-all
        
        WICHTIG: Für linear/inverse muss mindestens eines der folgenden Parameter gesetzt sein:
        - symbol (höchste Priorität)
        - baseCoin
        - settleCoin
        
        :param symbol: Optional - Symbol name (z.B. "XPINUSDT")
        :param base_coin: Optional - Base coin (z.B. "XPI")
        :param settle_coin: Optional - Settle coin (z.B. "USDT")
        :param order_filter: Optional - Order filter (Order, StopOrder, OpenOrder, etc.)
        :param timeout: Timeout in Sekunden (Standard: 5)
        :return: Liste von gecancelten Order-IDs oder None bei Fehler
        """
        url = f'{self.base_url}/v5/order/cancel-all'
        recv_window = '20000'
        
        try:
            import time as time_module
            start_time = time_module.time()
            
            # Request Body (POST)
            body = {
                'category': 'linear',
            }
            
            # Priorität: symbol > baseCoin > settleCoin
            if symbol:
                body['symbol'] = symbol
            elif base_coin:
                body['baseCoin'] = base_coin
            elif settle_coin:
                body['settleCoin'] = settle_coin
            else:
                logger.error(f"[CANCEL-ALL-ORDERS-DIRECT] ❌ Für linear muss mindestens symbol, baseCoin oder settleCoin gesetzt sein")
                return None
            
            if order_filter:
                body['orderFilter'] = order_filter
            
            # Erstelle Signatur (POST-Request: timestamp + api_key + recv_window + body_json)
            # WICHTIG: Verwende json.dumps() OHNE separators, um konsistent mit anderen erfolgreichen Funktionen zu sein
            timestamp = str(int(time.time() * 1000))
            body_json = json.dumps(body)  # Ohne separators, wie in set_tp_short_order
            signature_payload = f"{timestamp}{self.api_key}{recv_window}{body_json}".encode('utf-8')
            signature = hmac.new(
                self.secret_key.encode('utf-8'),
                signature_payload,
                hashlib.sha256
            ).hexdigest()
            
            # Header
            headers = {
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-TIMESTAMP': timestamp,
                'X-BAPI-RECV-WINDOW': recv_window,
                'X-BAPI-SIGN': signature,
                'Content-Type': 'application/json',
            }
            
            # WICHTIG: Verwende data=body_json statt json=body, damit die Signatur mit dem tatsächlich gesendeten Body übereinstimmt
            # (requests.post mit json= serialisiert das Dict selbst, was zu einer anderen Signatur führen kann)
            response = requests.post(url, headers=headers, data=body_json, timeout=timeout)
            
            elapsed = time_module.time() - start_time
            logger.debug(f"[CANCEL-ALL-ORDERS-DIRECT] Request abgeschlossen nach {elapsed:.2f}s")
            
            if response.status_code == 200:
                response_json = response.json()
                if response_json.get("retCode") == 0:
                    result = response_json.get("result", {})
                    cancelled_orders = result.get("list", [])
                    success = result.get("success", "0")
                    
                    if success == "1":
                        order_ids = [order.get("orderId", "") for order in cancelled_orders]
                        logger.info(f"[CANCEL-ALL-ORDERS-DIRECT] ✅ {len(order_ids)} Orders erfolgreich gecancelt")
                        return order_ids
                    else:
                        logger.warning(f"[CANCEL-ALL-ORDERS-DIRECT] ⚠️ Cancel-Request akzeptiert, aber success=0")
                        return []
                else:
                    ret_code = response_json.get("retCode")
                    ret_msg = response_json.get("retMsg", "Unknown error")
                    logger.warning(f"[CANCEL-ALL-ORDERS-DIRECT] Bybit API Fehler (retCode={ret_code}): {ret_msg}")
                    return []
            else:
                logger.error(f"[CANCEL-ALL-ORDERS-DIRECT] HTTP-Fehler: {response.status_code} - {response.text[:200]}")
                return []
                
        except requests.exceptions.Timeout:
            logger.error(f"[CANCEL-ALL-ORDERS-DIRECT] ⏱️ Timeout nach {timeout}s")
            return []
        except Exception as e:
            logger.error(f"[CANCEL-ALL-ORDERS-DIRECT] ❌ Fehler beim Canceln aller Orders: {e}", exc_info=True)
            return []

    def cancel_all_orders(self, symbol: str, timeout: float = 8.0, poll_interval: float = 0.2) -> bool:
        """
        Cancelt ALLE offenen Orders für ein Symbol und wartet per REST,
        bis das Orderbuch für das Symbol leer ist.

        - cancel-all ohne orderFilter (inkl. TP/SL/Conditional)
        - Polling über fetch_open_orders_direct() bis leer oder Timeout

        :param symbol: Trading-Symbol (z.B. "BTCUSDT")
        :param timeout: Maximale Wartezeit in Sekunden
        :param poll_interval: Polling-Intervall in Sekunden
        :return: True nur wenn vollständig leer bestätigt, sonst False
        """
        if not symbol:
            logger.error("[CANCEL-ALL-ORDERS] ❌ Symbol fehlt")
            return False

        logger.info(f"[CANCEL-ALL-ORDERS] 🧹 Starte Full-Cancel für {symbol}")

        # Nutzt /v5/order/cancel-all ohne orderFilter und prüft retCode intern.
        cancel_ok = self.cancel_all_orders_complete(symbol)
        if not cancel_ok:
            logger.error(f"[CANCEL-ALL-ORDERS] ❌ cancel-all fehlgeschlagen für {symbol}")
            return False

        deadline = time.time() + float(timeout)
        while time.time() <= deadline:
            try:
                open_orders = self.fetch_open_orders_direct(symbol, timeout=5)
            except Exception as exc:
                logger.warning(f"[CANCEL-ALL-ORDERS] Poll-Fehler: {exc}")
                open_orders = []

            if not open_orders:
                logger.info(f"[CANCEL-ALL-ORDERS] ✅ Orderbuch leer bestätigt für {symbol}")
                return True

            time.sleep(float(poll_interval))

        try:
            still_open = self.fetch_open_orders_direct(symbol, timeout=5) or []
            pending = len(still_open)
        except Exception:
            pending = -1
        logger.warning(
            f"[CANCEL-ALL-ORDERS] ⚠️ Timeout: Orderbuch nicht leer für {symbol}"
            + (f" (pending={pending})" if pending >= 0 else "")
        )
        return False

    def fetch_ticker_direct(self, symbol, timeout=5):
        """
        Holt Ticker-Daten direkt über Bybit API v5 mit explizitem Timeout.
        Dies ist schneller und zuverlässiger als CCXT.
        
        Dokumentation: https://bybit-exchange.github.io/docs/v5/market/tickers
        
        :param symbol: Handelssymbol (z.B. "XPINUSDT")
        :param timeout: Timeout in Sekunden (Standard: 5)
        :return: Ticker-Daten im CCXT-Format oder None bei Fehler
        """
        url = f'{self.base_url}/v5/market/tickers'
        recv_window = '20000'
        
        try:
            import time as time_module
            import urllib.parse
            start_time = time_module.time()
            
            # Query-Parameter (gemäß Bybit-Dokumentation)
            params = {
                'category': 'linear',
                'symbol': symbol,
            }
            
            # Erstelle Query-String für Signatur (sortiert, URL-encoded)
            sorted_params = sorted(params.items())
            query_parts = []
            for key, value in sorted_params:
                encoded_key = urllib.parse.quote(str(key), safe='')
                encoded_value = urllib.parse.quote(str(value), safe='')
                query_parts.append(f"{encoded_key}={encoded_value}")
            query_string = '&'.join(query_parts)
            
            # Erstelle Signatur (GET-Request: timestamp + api_key + recv_window + query_string)
            timestamp = str(int(time.time() * 1000))
            signature_payload = f"{timestamp}{self.api_key}{recv_window}{query_string}".encode('utf-8')
            signature = hmac.new(
                self.secret_key.encode('utf-8'),
                signature_payload,
                hashlib.sha256
            ).hexdigest()
            
            # Header
            headers = {
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-TIMESTAMP': timestamp,
                'X-BAPI-RECV-WINDOW': recv_window,
                'X-BAPI-SIGN': signature,
            }
            
            # FIX: Session verwenden für bessere Performance + Tuple-Timeout
            response = self._session.get(
                url, 
                headers=headers, 
                params=params, 
                timeout=(2, timeout)  # (connect_timeout=2s, read_timeout=5s)
            )
            
            elapsed = time_module.time() - start_time
            if elapsed > 3.0:
                logger.warning(
                    f"[FETCH-TICKER-DIRECT] ⚠️ Langsame Antwort ({elapsed:.2f}s) - "
                    f"möglicherweise hohe Volatilität bei Bybit"
                )
            logger.debug(f"[FETCH-TICKER-DIRECT] Request abgeschlossen nach {elapsed:.2f}s")
            
            if response.status_code == 200:
                response_json = response.json()
                if response_json.get("retCode") == 0:
                    ticker_list = response_json.get("result", {}).get("list", [])
                    
                    if not ticker_list:
                        logger.warning(
                            f"[FETCH-TICKER-DIRECT] Keine Ticker-Daten für {symbol} gefunden. "
                            "Bybit Linear führt dieses Symbol möglicherweise nicht (mehr) – bitte Symbol prüfen (z.B. LUNCUSDT statt LUNAUSDT)."
                        )
                        return None
                    
                    # Nimm den ersten Ticker (sollte nur einer sein bei symbol-spezifischer Abfrage)
                    ticker_data = ticker_list[0]
                    
                    # Konvertiere Bybit-Format zu CCXT-Format für Kompatibilität
                    ccxt_ticker = {
                        'symbol': ticker_data.get('symbol', symbol),
                        'last': float(ticker_data.get('lastPrice', 0) or 0),
                        'close': float(ticker_data.get('lastPrice', 0) or 0),  # Alias für last
                        'bid': float(ticker_data.get('bid1Price', 0) or 0),
                        'ask': float(ticker_data.get('ask1Price', 0) or 0),
                        'high': float(ticker_data.get('highPrice24h', 0) or 0),
                        'low': float(ticker_data.get('lowPrice24h', 0) or 0),
                        'open': float(ticker_data.get('prevPrice24h', 0) or 0),
                        'percentage': float(ticker_data.get('price24hPcnt', 0) or 0) * 100,  # Konvertiere zu Prozent
                        'baseVolume': float(ticker_data.get('volume24h', 0) or 0),
                        'quoteVolume': float(ticker_data.get('turnover24h', 0) or 0),
                        'info': ticker_data,  # Behalte Original-Info für vollständige Kompatibilität
                    }
                    
                    logger.debug(f"[FETCH-TICKER-DIRECT] Ticker-Daten für {symbol} abgerufen: lastPrice={ccxt_ticker['last']}")
                    return ccxt_ticker
                else:
                    ret_code = response_json.get("retCode")
                    ret_msg = response_json.get("retMsg", "Unknown error")
                    logger.warning(f"[FETCH-TICKER-DIRECT] Bybit API Fehler (retCode={ret_code}): {ret_msg}")
                    return None
            else:
                logger.error(f"[FETCH-TICKER-DIRECT] HTTP-Fehler: {response.status_code} - {response.text[:200]}")
                return None
                
        except requests.exceptions.ConnectTimeout:
            logger.error(f"[FETCH-TICKER-DIRECT] ⏱️ Connect-Timeout nach 2s für {symbol}")
            return None
        except requests.exceptions.ReadTimeout:
            logger.error(f"[FETCH-TICKER-DIRECT] ⏱️ Read-Timeout nach {timeout}s für {symbol}")
            return None
        except requests.exceptions.Timeout:
            logger.error(f"[FETCH-TICKER-DIRECT] ⏱️ Timeout nach {timeout}s für {symbol}")
            return None
        except Exception as e:
            logger.error(f"[FETCH-TICKER-DIRECT] ❌ Fehler beim Abrufen des Tickers für {symbol}: {e}", exc_info=True)
            return None

    def fetch_open_orders_direct(self, symbol, timeout=5, order_filter=None):
        """
        Holt offene Orders direkt über Bybit API v5 mit explizitem Timeout.
        Dies ist schneller und zuverlässiger als CCXT, besonders für ReadOnly-Checks.
        
        Dokumentation: https://bybit-exchange.github.io/docs/v5/order/open-order
        
        :param symbol: Handelssymbol (z.B. "XPINUSDT")
        :param timeout: Timeout in Sekunden (Standard: 5)
        :param order_filter: Optional - Order, StopOrder, tpslOrder, etc. (Standard: 'StopOrder' für TP/SL-Prüfung)
        :return: Liste von Orders im CCXT-Format oder leere Liste bei Fehler
        """
        url = f'{self.base_url}/v5/order/realtime'
        recv_window = '20000'
        
        try:
            import time as time_module
            import urllib.parse
            start_time = time_module.time()
            
            # FIX 1: Type-Korrektur - Integer statt String (gemäß Bybit-Dokumentation)
            # FIX 2: orderFilter standardmäßig auf 'StopOrder' setzen (nur TP/SL-Orders für Prüfung)
            if order_filter is None:
                order_filter = 'StopOrder'  # Standard: Nur StopOrders (TP/SL) für bessere Performance
            
            # Query-Parameter (gemäß Bybit-Dokumentation)
            params = {
                'category': 'linear',
                'symbol': symbol,
                'openOnly': 0,  # FIX: Integer statt String - 0 = offene Orders (New, PartiallyFilled)
                'limit': 50,    # FIX: Integer statt String - Max 50 Orders pro Request (Bybit-Limit)
                'orderFilter': order_filter  # FIX: Immer setzen für bessere Performance
            }
            
            # Erstelle Query-String für Signatur (sortiert, URL-encoded)
            # WICHTIG: Parameter müssen sortiert sein für korrekte Signatur
            sorted_params = sorted(params.items())
            query_parts = []
            for key, value in sorted_params:
                # URL-encode Parameter (Integer werden zu String konvertiert)
                encoded_key = urllib.parse.quote(str(key), safe='')
                encoded_value = urllib.parse.quote(str(value), safe='')
                query_parts.append(f"{encoded_key}={encoded_value}")
            query_string = '&'.join(query_parts)
            
            # Erstelle Signatur (GET-Request: timestamp + api_key + recv_window + query_string)
            timestamp = str(int(time.time() * 1000))
            signature_payload = f"{timestamp}{self.api_key}{recv_window}{query_string}".encode('utf-8')
            signature = hmac.new(
                self.secret_key.encode('utf-8'),
                signature_payload,
                hashlib.sha256
            ).hexdigest()
            
            # Header
            headers = {
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-TIMESTAMP': timestamp,
                'X-BAPI-RECV-WINDOW': recv_window,
                'X-BAPI-SIGN': signature,
            }
            
            # WICHTIG: Verwende query_string direkt in der URL, nicht params=params
            # Dies stellt sicher, dass die Signatur mit dem tatsächlichen Request übereinstimmt
            # requests.get() mit params=params encodet Parameter anders als unser manueller Query-String
            full_url = f"{url}?{query_string}"
            
            # FIX 3: Tuple-Timeout (connect_timeout=2s, read_timeout=timeout)
            # FIX 4: Session verwenden für bessere Performance (spart TCP/TLS-Handshake)
            response = self._session.get(
                full_url, 
                headers=headers, 
                timeout=(2, timeout)  # (connect_timeout=2s, read_timeout=5s)
            )
            
            elapsed = time_module.time() - start_time
            # FIX 5: Logging bei langsamen Antworten (mögliche Volatilität)
            if elapsed > 3.0:
                logger.warning(
                    f"[FETCH-ORDERS-DIRECT] ⚠️ Langsame Antwort ({elapsed:.2f}s) für {symbol} - "
                    f"möglicherweise hohe Volatilität bei Bybit"
                )
            logger.debug(f"[FETCH-ORDERS-DIRECT] Request abgeschlossen nach {elapsed:.2f}s")
            
            if response.status_code == 200:
                response_json = response.json()
                if response_json.get("retCode") == 0:
                    orders = response_json.get("result", {}).get("list", [])
                    
                    # Konvertiere Bybit-Format zu CCXT-Format für Kompatibilität
                    # WICHTIG: Behalte alle wichtigen Felder für TP/SL-Erkennung
                    ccxt_orders = []
                    for order in orders:
                        # Extrahiere wichtige Felder für Kompatibilität
                        order_id = order.get('orderId', '')
                        stop_order_type = order.get('stopOrderType', 'UNKNOWN')
                        side = order.get('side', '')
                        position_idx = int(order.get('positionIdx', 0))
                        trigger_price = order.get('triggerPrice', '0')
                        tp_limit_price = order.get('tpLimitPrice', '')
                        sl_limit_price = order.get('slLimitPrice', '')
                        order_type = order.get('orderType', '')
                        order_status = order.get('orderStatus', '')
                        
                        # CCXT-kompatibles Format
                        ccxt_order = {
                            'id': order_id,
                            'symbol': symbol,
                            'type': order_type.lower() if order_type else 'unknown',
                            'side': side,
                            'amount': float(order.get('qty', 0) or 0),
                            'price': float(order.get('price', 0) or 0),
                            'status': order_status,
                            'info': order  # Behalte Original-Info für vollständige Kompatibilität
                        }
                        
                        # Füge wichtige Felder hinzu, die für TP/SL-Erkennung benötigt werden
                        # Diese werden im 'info'-Dict gespeichert, aber auch direkt zugänglich gemacht
                        if 'info' in ccxt_order:
                            ccxt_order['info']['stopOrderType'] = stop_order_type
                            ccxt_order['info']['positionIdx'] = position_idx
                            ccxt_order['info']['triggerPrice'] = trigger_price
                            ccxt_order['info']['tpLimitPrice'] = tp_limit_price
                            ccxt_order['info']['slLimitPrice'] = sl_limit_price
                        
                        ccxt_orders.append(ccxt_order)
                    
                    logger.debug(f"[FETCH-ORDERS-DIRECT] {len(ccxt_orders)} Orders gefunden für {symbol}")
                    return ccxt_orders
                else:
                    ret_code = response_json.get("retCode")
                    ret_msg = response_json.get("retMsg", "Unknown error")
                    logger.warning(f"[FETCH-ORDERS-DIRECT] Bybit API Fehler (retCode={ret_code}): {ret_msg}")
                    return []
            else:
                logger.error(f"[FETCH-ORDERS-DIRECT] HTTP-Fehler: {response.status_code} - {response.text[:200]}")
                return []
                
        except requests.exceptions.ConnectTimeout:
            logger.error(f"[FETCH-ORDERS-DIRECT] ⏱️ Connect-Timeout nach 2s für {symbol}")
            return []
        except requests.exceptions.ReadTimeout:
            logger.error(f"[FETCH-ORDERS-DIRECT] ⏱️ Read-Timeout nach {timeout}s für {symbol}")
            return []
        except requests.exceptions.Timeout:
            logger.error(f"[FETCH-ORDERS-DIRECT] ⏱️ Timeout nach {timeout}s für {symbol}")
            return []
        except Exception as e:
            logger.error(f"[FETCH-ORDERS-DIRECT] ❌ Fehler beim Abrufen der Orders: {e}", exc_info=True)
            return []

    def fetch_positions_direct(self, symbol=None, timeout=5, settle_coin=None):
        """
        Holt Positionen direkt über Bybit API v5 mit explizitem Timeout.
        Dies ist schneller und zuverlässiger als CCXT.
        
        Dokumentation: https://bybit-exchange.github.io/docs/v5/position/list
        
        :param symbol: Optional - Handelssymbol (z.B. "XPINUSDT"). Wenn None, werden alle Positionen geholt
        :param timeout: Timeout in Sekunden (Standard: 5)
        :param settle_coin: Optional - Settle Coin (z.B. "USDT" für linear)
        :return: Liste von Positionen im CCXT-Format oder leere Liste bei Fehler
        """
        url = f'{self.base_url}/v5/position/list'
        recv_window = '20000'
        
        try:
            import time as time_module
            import urllib.parse
            start_time = time_module.time()
            
            # Query-Parameter (gemäß Bybit-Dokumentation)
            params = {
                'category': 'linear',
            }
            
            # Symbol hat höchste Priorität
            if symbol:
                params['symbol'] = symbol
            elif settle_coin:
                params['settleCoin'] = settle_coin
            else:
                # WICHTIG: Bybit API erfordert entweder symbol oder settleCoin
                # Wenn beide None sind, verwende USDT als Standard-SettleCoin
                # Dies ermöglicht das Abrufen aller Positionen
                params['settleCoin'] = 'USDT'
            
            # Erstelle Query-String für Signatur (sortiert, URL-encoded)
            sorted_params = sorted(params.items())
            query_parts = []
            for key, value in sorted_params:
                encoded_key = urllib.parse.quote(str(key), safe='')
                encoded_value = urllib.parse.quote(str(value), safe='')
                query_parts.append(f"{encoded_key}={encoded_value}")
            query_string = '&'.join(query_parts)
            
            # Erstelle Signatur (GET-Request: timestamp + api_key + recv_window + query_string)
            timestamp = str(int(time.time() * 1000))
            signature_payload = f"{timestamp}{self.api_key}{recv_window}{query_string}".encode('utf-8')
            signature = hmac.new(
                self.secret_key.encode('utf-8'),
                signature_payload,
                hashlib.sha256
            ).hexdigest()
            
            # Header
            headers = {
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-TIMESTAMP': timestamp,
                'X-BAPI-RECV-WINDOW': recv_window,
                'X-BAPI-SIGN': signature,
            }
            
            # FIX: Session verwenden für bessere Performance + Tuple-Timeout
            response = self._session.get(
                url, 
                headers=headers, 
                params=params, 
                timeout=(2, timeout)  # (connect_timeout=2s, read_timeout=5s)
            )
            
            elapsed = time_module.time() - start_time
            if elapsed > 3.0:
                logger.warning(
                    f"[FETCH-POSITIONS-DIRECT] ⚠️ Langsame Antwort ({elapsed:.2f}s) - "
                    f"möglicherweise hohe Volatilität bei Bybit"
                )
            logger.debug(f"[FETCH-POSITIONS-DIRECT] Request abgeschlossen nach {elapsed:.2f}s")
            
            if response.status_code == 200:
                response_json = response.json()
                if response_json.get("retCode") == 0:
                    positions = response_json.get("result", {}).get("list", [])
                    if not positions and symbol:
                        logger.info(f"[FETCH-POSITIONS-DIRECT] Bybit liefert 0 Positionen für symbol={symbol} (Account könnte keine offene Position haben)")
                    
                    # Konvertiere Bybit-Format zu CCXT-Format für Kompatibilität
                    ccxt_positions = []
                    for pos in positions:
                        # Extrahiere wichtige Felder
                        position_idx = int(pos.get('positionIdx', 0))
                        side = pos.get('side', '')
                        size = float(pos.get('size', 0) or 0)
                        avg_price = float(pos.get('avgPrice', 0) or 0)
                        symbol_pos = pos.get('symbol', '')
                        
                        # CCXT-kompatibles Format
                        ccxt_position = {
                            'info': pos,  # Behalte Original-Info für vollständige Kompatibilität
                            'symbol': symbol_pos,
                            'side': side,
                            'size': size,
                            'contracts': size,
                            'entryPrice': avg_price,
                            'avgPrice': avg_price,
                            'positionIdx': position_idx,
                            'markPrice': float(pos.get('markPrice', 0) or 0),
                            'unrealisedPnl': float(pos.get('unrealisedPnl', 0) or 0),
                            'leverage': float(pos.get('leverage', 0) or 0) if pos.get('leverage') else 0,
                            'liquidationPrice': float(pos.get('liqPrice', 0) or 0) if pos.get('liqPrice') else None,
                            'takeProfit': float(pos.get('takeProfit', 0) or 0) if pos.get('takeProfit') else None,
                            'stopLoss': float(pos.get('stopLoss', 0) or 0) if pos.get('stopLoss') else None,
                        }
                        
                        ccxt_positions.append(ccxt_position)
                    
                    logger.debug(f"[FETCH-POSITIONS-DIRECT] {len(ccxt_positions)} Positionen gefunden")
                    return ccxt_positions
                else:
                    ret_code = response_json.get("retCode")
                    ret_msg = response_json.get("retMsg", "Unknown error")
                    logger.warning(f"[FETCH-POSITIONS-DIRECT] Bybit API Fehler (retCode={ret_code}): {ret_msg}")
                    return []
            else:
                logger.error(f"[FETCH-POSITIONS-DIRECT] HTTP-Fehler: {response.status_code} - {response.text[:200]}")
                return []
                
        except requests.exceptions.ConnectTimeout:
            logger.error(f"[FETCH-POSITIONS-DIRECT] ⏱️ Connect-Timeout nach 2s")
            return []
        except requests.exceptions.ReadTimeout:
            logger.error(f"[FETCH-POSITIONS-DIRECT] ⏱️ Read-Timeout nach {timeout}s")
            return []
        except requests.exceptions.Timeout:
            logger.error(f"[FETCH-POSITIONS-DIRECT] ⏱️ Timeout nach {timeout}s")
            return []
        except Exception as e:
            logger.error(f"[FETCH-POSITIONS-DIRECT] ❌ Fehler beim Abrufen der Positionen: {e}", exc_info=True)
            return []

    def get_all_open_positions(self):
        """
        Holt alle offenen Positionen (Long oder Short) von der Exchange.
        Returns: Liste von Dictionaries mit Symbol, Side, Size, AvgPrice
        """
        try:
            logger.info("Hole alle offenen Positionen von der Exchange...")
            # WICHTIG: Verwende fetch_positions_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
            # Ohne Symbol werden alle Positionen geholt
            positions = self.fetch_positions_direct(symbol=None, timeout=5)
            logger.info(f"Empfangen: {len(positions)} Positionen von der Exchange")
            
            open_positions = []
            for pos in positions:
                try:
                    # Direkte API gibt Positionen direkt zurück (nicht in 'info')
                    info = pos.get("info", {}) if "info" in pos else pos
                    size = float(info.get("size", 0) or 0)
                    if size > 0:  # Nur Positionen mit Size > 0
                        symbol_pos = info.get("symbol", "")
                        side = info.get("side", "")
                        avg_price = info.get("avgPrice") or info.get("entryPrice") or info.get("avgPrice")
                        avg_price = float(avg_price or 0)
                        open_positions.append({
                            "symbol": symbol_pos,
                            "side": side,
                            "size": size,
                            "avg_price": avg_price
                        })
                        logger.info(f"  Position gefunden: {symbol_pos} {side} Size={size} AvgPrice={avg_price}")
                except Exception as e:
                    logger.warning(f"Fehler beim Verarbeiten einer Position: {e}")
                    continue
            
            logger.info(f"Insgesamt {len(open_positions)} offene Positionen mit Size > 0 gefunden")
            return open_positions
        except Exception as e:
            error_msg = str(e)
            if "timeout" in error_msg.lower() or "connection" in error_msg.lower():
                logger.error(f"❌ Netzwerk-Fehler beim Abrufen aller Positionen: {e}")
                logger.error("💡 Tipp: In Tanzania ist Bybit nur über VPN erreichbar!")
                logger.error("   Aktiviere VPN und starte die Services neu.")
            else:
                logger.error(f"Fehler beim Abrufen aller Positionen: {e}", exc_info=True)
            return []

    def get_symbol_info(self, symbol):
        """
        Holt Symbol-Informationen (qtyStep, minQty) von Bybit.
        """
        try:
            url = f"{self.base_url}/v5/market/instruments-info?category=linear&symbol={symbol}"
            response = requests.get(url)
            if response.status_code == 200:
                data = response.json()
                if data.get("retCode") == 0:
                    symbol_info = data.get("result", {}).get("list", [{}])[0]
                    lot_size_filter = symbol_info.get("lotSizeFilter", {})
                    qty_step = float(lot_size_filter.get("qtyStep", "0.01"))
                    min_qty = float(lot_size_filter.get("minQty", "0"))
                    return qty_step, min_qty
            return None, None
        except Exception as e:
            logger.error(f"Fehler beim Abrufen der Symbol-Info für {symbol}: {e}", exc_info=True)
            return None, None

    def get_symbol_tick_size(self, symbol):
        """
        Holt den Tick-Size für ein Symbol (From priceFilter).
        """
        cached = self._symbol_tick_cache.get(symbol)
        if cached:
            return cached
        try:
            url = f"{self.base_url}/v5/market/instruments-info?category=linear&symbol={symbol}"
            response = requests.get(url)
            if response.status_code == 200:
                data = response.json()
                if data.get("retCode") == 0:
                    symbol_info = data.get("result", {}).get("list", [{}])[0]
                    price_filter = symbol_info.get("priceFilter", {})
                    tick_size = float(price_filter.get("tickSize", "0.01"))
                    self._symbol_tick_cache[symbol] = tick_size
                    return tick_size
            return None
        except Exception as e:
            logger.error(f"Fehler beim Abrufen des Tick-Size für {symbol}: {e}", exc_info=True)
            return None
    
    def get_account_equity(self):
        """
        Holt die Account Equity (Wallet Balance) von Bybit.
        Returns: float (Equity in USDT) oder None bei Fehler
        """
        try:
            url = f'{self.base_url}/v5/account/wallet-balance'
            recv_window = '20000'  # Erhöht für VPN-Latenz
            
            # Für GET-Requests: Query-Parameter als String für Signatur
            params = {
                "accountType": "UNIFIED",
                "coin": "USDT,USDC"
            }
            
            # Erstelle Query-String für Signatur (alphabetisch sortiert)
            query_string = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
            
            timestamp = str(int(time.time() * 1000))
            # Für GET: timestamp + api_key + recv_window + query_string
            params_to_sign = timestamp + self.api_key + recv_window + query_string
            signature = hmac.new(
                self.secret_key.encode('utf-8'),
                params_to_sign.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            
            headers = {
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-SIGN': signature,
                'X-BAPI-SIGN-TYPE': '2',
                'X-BAPI-TIMESTAMP': timestamp,
                'X-BAPI-RECV-WINDOW': recv_window
            }
            
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            result = response.json()
            
            if result.get('retCode') == 0:
                account_data = result.get('result', {}).get('list', [{}])[0]
                total_equity = float(account_data.get('totalEquity', 0))
                logger.info(f"Account Equity: {total_equity} USDT")
                return total_equity
            else:
                logger.error(f"Bybit API Fehler beim Abrufen der Equity: {result.get('retMsg', 'Unbekannter Fehler')}")
                return None
                
        except Exception as e:
            logger.error(f"Fehler beim Abrufen der Account Equity: {e}", exc_info=True)
            return None

    def get_account_margin_balance(self):
        """
        Holt die Margin Balance (totalMarginBalance) von Bybit.
        Returns: float (Margin Balance in USDT) oder None bei Fehler
        """
        try:
            url = f'{self.base_url}/v5/account/wallet-balance'
            recv_window = '20000'
            params = {
                "accountType": "UNIFIED"
            }
            query_string = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
            timestamp = str(int(time.time() * 1000))
            params_to_sign = timestamp + self.api_key + recv_window + query_string
            signature = hmac.new(
                self.secret_key.encode('utf-8'),
                params_to_sign.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            headers = {
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-SIGN': signature,
                'X-BAPI-SIGN-TYPE': '2',
                'X-BAPI-TIMESTAMP': timestamp,
                'X-BAPI-RECV-WINDOW': recv_window
            }
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            result = response.json()
            if result.get('retCode') == 0:
                account_data = result.get('result', {}).get('list', [{}])[0]
                raw_value = (
                    account_data.get('totalMarginBalance')
                    or account_data.get('totalWalletBalance')
                    or account_data.get('totalEquity')
                    or 0
                )
                margin_balance = float(raw_value)
                logger.info(f"Account Margin Balance: {margin_balance} USDT")
                return margin_balance
            else:
                logger.error(f"Bybit API Fehler beim Abrufen der Margin Balance: {result.get('retMsg', 'Unbekannter Fehler')}")
                return None
        except Exception as e:
            logger.error(f"Fehler beim Abrufen der Margin Balance: {e}", exc_info=True)
            return None

    def get_account_available_balance(self):
        """
        Holt die Available Balance (totalAvailableBalance) von Bybit.
        Verfügbar für neue Orders = totalMarginBalance - totalInitialMargin (vereinfacht).
        Returns: float (Available Balance in USDT) oder None bei Fehler
        """
        try:
            url = f'{self.base_url}/v5/account/wallet-balance'
            recv_window = '20000'
            params = {"accountType": "UNIFIED"}
            query_string = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
            timestamp = str(int(time.time() * 1000))
            params_to_sign = timestamp + self.api_key + recv_window + query_string
            signature = hmac.new(
                self.secret_key.encode('utf-8'),
                params_to_sign.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            headers = {
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-SIGN': signature,
                'X-BAPI-SIGN-TYPE': '2',
                'X-BAPI-TIMESTAMP': timestamp,
                'X-BAPI-RECV-WINDOW': recv_window
            }
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            result = response.json()
            if result.get('retCode') == 0:
                account_data = result.get('result', {}).get('list', [{}])[0]
                raw = account_data.get('totalAvailableBalance') or account_data.get('totalMarginBalance') or 0
                return float(raw)
            return None
        except Exception as e:
            logger.error(f"Fehler beim Abrufen der Available Balance: {e}", exc_info=True)
            return None

    def get_closed_pnl(self, category="linear", symbol=None, limit=100, start_time=None, end_time=None):
        """
        Holt Closed PnL History von Bybit.
        GET /v5/position/closed-pnl
        Returns: list of closed PnL records oder [] bei Fehler
        """
        try:
            url = f'{self.base_url}/v5/position/closed-pnl'
            recv_window = '20000'
            params = {"category": category, "limit": limit}
            if symbol:
                params["symbol"] = symbol
            if start_time:
                params["startTime"] = start_time
            if end_time:
                params["endTime"] = end_time

            query_string = "&".join([f"{k}={v}" for k, v in sorted(params.items())])
            timestamp = str(int(time.time() * 1000))
            params_to_sign = timestamp + self.api_key + recv_window + query_string
            signature = hmac.new(
                self.secret_key.encode('utf-8'),
                params_to_sign.encode('utf-8'),
                hashlib.sha256
            ).hexdigest()
            headers = {
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-SIGN': signature,
                'X-BAPI-SIGN-TYPE': '2',
                'X-BAPI-TIMESTAMP': timestamp,
                'X-BAPI-RECV-WINDOW': recv_window
            }
            response = requests.get(url, headers=headers, params=params, timeout=15)
            response.raise_for_status()
            result = response.json()
            if result.get('retCode') == 0:
                return result.get('result', {}).get('list', [])
            logger.error(f"Bybit API Fehler get_closed_pnl: {result.get('retMsg', 'Unbekannter Fehler')}")
            return []
        except Exception as e:
            logger.error(f"Fehler beim Abrufen der Closed PnL: {e}", exc_info=True)
            return []

    def get_position_pnl(self, symbol):
        """
        Holt den aktuellen unrealisierten Profit/Loss für eine Position.
        Returns: dict mit 'unrealised_pnl', 'mark_price', 'size', 'entry_price', 'tp_price' oder None
        """
        try:
            # WICHTIG: Verwende fetch_positions_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
            positions = self.fetch_positions_direct(symbol=symbol, timeout=5)
            for pos in positions:
                info = pos.get("info", {})
                size = float(info.get("size", 0) or 0)
                if size > 0:
                    unrealised_pnl = float(info.get("unrealisedPnl", 0) or 0)
                    mark_price = float(info.get("markPrice", 0) or 0)
                    entry_price = float(info.get("avgPrice") or info.get("entryPrice") or 0)
                    side = info.get("side", "")
                    tp_price = None
                    try:
                        tp_price_str = info.get("takeProfit", None)
                        if tp_price_str:
                            tp_price = float(tp_price_str)
                    except:
                        pass
                    
                    return {
                        'unrealised_pnl': unrealised_pnl,
                        'mark_price': mark_price,
                        'size': size,
                        'entry_price': entry_price,
                        'side': side,
                        'tp_price': tp_price
                    }
            return None
        except Exception as e:
            logger.error(f"Fehler beim Abrufen des PnL für {symbol}: {e}", exc_info=True)
            return None
    
    def get_tp_price(self, symbol, position_idx=1):
        """
        Holt den Take-Profit-Preis für eine Position.
        Versucht zuerst aus Position-Info, dann aus Open Orders.
        Returns: float (TP-Preis) oder None
        """
        try:
            # Versuche aus Position-Info
            # WICHTIG: Verwende fetch_positions_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
            positions = self.fetch_positions_direct(symbol=symbol, timeout=5)
            for pos in positions:
                info = pos.get("info", {})
                pos_idx = int(info.get("positionIdx", 0))
                size = float(info.get("size", 0) or 0)
                if size > 0 and pos_idx == position_idx:
                    tp_price_str = info.get("takeProfit", None)
                    if tp_price_str:
                        try:
                            return float(tp_price_str)
                        except:
                            pass
            
            # Versuche aus Open Orders
            # WICHTIG: Verwende fetch_open_orders_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
            open_orders = self.fetch_open_orders_direct(symbol, timeout=5)
            for order in open_orders:
                order_info = order.get("info", {})
                stop_order_type = order_info.get("stopOrderType", "")
                order_pos_idx = int(order_info.get("positionIdx", 0))
                
                if stop_order_type in ["TakeProfit", "PartialTakeProfit"] and order_pos_idx == position_idx:
                    # TP Order gefunden
                    tp_limit_price = order_info.get("tpLimitPrice", None)
                    trigger_price = order_info.get("triggerPrice", None)
                    price = order.get("price", None) or order_info.get("price", None)
                    
                    # Prefer tpLimitPrice, then price, then triggerPrice
                    if tp_limit_price:
                        try:
                            return float(tp_limit_price)
                        except:
                            pass
                    if price:
                        try:
                            return float(price)
                        except:
                            pass
                    if trigger_price:
                        try:
                            return float(trigger_price)
                        except:
                            pass
            
            return None
        except Exception as e:
            logger.error(f"Fehler beim Abrufen des TP-Preises für {symbol}: {e}", exc_info=True)
            return None
    
    def get_tp_sl_orders(self, symbol, position_idx=1):
        """
        Holt alle TP und SL Orders für eine Position mit Preisen und Anzahl.
        Returns: dict mit 'tp_orders' (Liste von Preisen), 'sl_orders' (Liste von Preisen), 
                 'tp_count', 'sl_count', 'tp_prices', 'sl_prices'
        """
        result = {
            'tp_orders': [],
            'sl_orders': [],
            'tp_count': 0,
            'sl_count': 0,
            'tp_prices': [],
            'sl_prices': []
        }
        
        try:
            logger.debug(f"get_tp_sl_orders: Hole Orders für {symbol}, position_idx={position_idx}")
            
            # Verwende Bybit REST API direkt für zuverlässigeres Abrufen von TP/SL Orders
            url = f'{self.base_url}/v5/order/realtime'
            recv_window = '20000'  # Erhöht für VPN-Latenz
            content_type = 'application/json'
            
            params = {
                "category": "linear",
                "symbol": symbol,
                "limit": 50
            }
            
            request_params = "&".join([f"{k}={v}" for k, v in params.items()])
            timestamp = str(int(time.time() * 1000))
            params_to_sign = timestamp + self.api_key + recv_window + request_params
            signature = hmac.new(
                self.secret_key.encode('utf-8'), params_to_sign.encode('utf-8'), hashlib.sha256
            ).hexdigest()
            
            headers = {
                'Content-Type': content_type,
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-RECV-WINDOW': recv_window,
                'X-BAPI-SIGN': signature,
                'X-BAPI-TIMESTAMP': timestamp
            }
            
            response = requests.get(f"{url}?{request_params}", headers=headers)
            
            if response.status_code != 200:
                logger.error(f"get_tp_sl_orders: HTTP-Fehler {response.status_code} beim Abrufen der Orders für {symbol}")
                return result
            
            response_json = response.json()
            if response_json.get("retCode") != 0:
                logger.error(f"get_tp_sl_orders: Bybit API Fehler: {response_json.get('retMsg')} (retCode: {response_json.get('retCode')})")
                return result
            
            orders_list = response_json.get("result", {}).get("list", [])
            logger.debug(f"get_tp_sl_orders: {len(orders_list)} offene Orders gefunden für {symbol}")
            
            for order_info in orders_list:
                stop_order_type = order_info.get("stopOrderType", "")
                order_pos_idx = int(order_info.get("positionIdx", 0))
                
                logger.debug(f"get_tp_sl_orders: Order gefunden - stopOrderType={stop_order_type}, positionIdx={order_pos_idx}, target={position_idx}")
                
                if order_pos_idx != position_idx:
                    logger.debug(f"get_tp_sl_orders: Order übersprungen (positionIdx {order_pos_idx} != {position_idx})")
                    continue
                
                # Bestimme den Preis
                tp_limit_price = order_info.get("tpLimitPrice", None)
                trigger_price = order_info.get("triggerPrice", None)
                price = order_info.get("price", None)
                
                order_price = None
                if stop_order_type in ["TakeProfit", "PartialTakeProfit"]:
                    # TP Order: prefer tpLimitPrice, then price, then triggerPrice
                    if tp_limit_price:
                        try:
                            order_price = float(tp_limit_price)
                        except:
                            pass
                    if not order_price and price:
                        try:
                            order_price = float(price)
                        except:
                            pass
                    if not order_price and trigger_price:
                        try:
                            order_price = float(trigger_price)
                        except:
                            pass
                    
                    if order_price:
                        result['tp_orders'].append({
                            'price': order_price,
                            'size': float(order_info.get("qty", 0) or 0),
                            'order_id': order_info.get("orderId", "N/A"),
                            'type': stop_order_type
                        })
                        result['tp_prices'].append(order_price)
                        result['tp_count'] += 1
                        logger.debug(f"get_tp_sl_orders: TP Order hinzugefügt - price={order_price}, size={order_info.get('qty', 0)}")
                        
                elif stop_order_type in ["StopLoss", "PartialStopLoss"]:
                    # SL Order: use trigger price
                    if trigger_price:
                        try:
                            order_price = float(trigger_price)
                        except:
                            pass
                    
                    if order_price:
                        result['sl_orders'].append({
                            'price': order_price,
                            'size': float(order_info.get("qty", 0) or 0),
                            'order_id': order_info.get("orderId", "N/A"),
                            'type': stop_order_type
                        })
                        result['sl_prices'].append(order_price)
                        result['sl_count'] += 1
                        logger.debug(f"get_tp_sl_orders: SL Order hinzugefügt - price={order_price}, size={order_info.get('qty', 0)}")
            
            # Sortiere Preise
            result['tp_prices'].sort()
            result['sl_prices'].sort()
            
            logger.debug(f"get_tp_sl_orders: Ergebnis für {symbol} (position_idx={position_idx}): tp_count={result['tp_count']}, sl_count={result['sl_count']}")
            
        except Exception as e:
            logger.error(f"Fehler beim Abrufen der TP/SL Orders für {symbol}: {e}", exc_info=True)
        
        return result
    
    def close_all_positions(self, symbol):
        """
        Schließt alle Positionen (Long und Short) für ein Symbol.
        Returns: dict mit 'success', 'long_closed', 'short_closed', 'errors'
        """
        result = {
            'success': False,
            'long_closed': False,
            'short_closed': False,
            'errors': []
        }
        
        try:
            # Hole aktuelle Positionen
            # WICHTIG: Verwende fetch_positions_direct() statt CCXT für schnelleres Timeout (5s statt 60s)
            positions = self.fetch_positions_direct(symbol=symbol, timeout=5)
            
            for pos in positions:
                info = pos.get("info", {})
                size = float(info.get("size", 0) or 0)
                if size <= 0:
                    continue
                
                side = info.get("side", "")
                position_idx = int(info.get("positionIdx", 0))
                
                # Schließe Position basierend auf positionIdx
                if position_idx == 1:  # Long-Position (Hedge Mode)
                    logger.info(f"Schließe Long-Position: {size} Coins (positionIdx=1)")
                    closed = self.close_partial_long(symbol, size)
                    if closed:
                        result['long_closed'] = True
                    else:
                        result['errors'].append("Fehler beim Schließen der Long-Position")
                elif position_idx == 2:  # Short-Position (Hedge Mode)
                    logger.info(f"Schließe Short-Position: {size} Coins (positionIdx=2)")
                    closed = self.close_partial_short(symbol, size)
                    if closed:
                        result['short_closed'] = True
                    else:
                        result['errors'].append("Fehler beim Schließen der Short-Position")
                else:
                    # Fallback: Verwende side wenn positionIdx nicht verfügbar
                    if side == "Buy":  # Long-Position
                        logger.info(f"Schließe Long-Position: {size} Coins (Fallback: side=Buy)")
                        closed = self.close_partial_long(symbol, size)
                        if closed:
                            result['long_closed'] = True
                        else:
                            result['errors'].append("Fehler beim Schließen der Long-Position")
                    elif side == "Sell":  # Short-Position
                        logger.info(f"Schließe Short-Position: {size} Coins (Fallback: side=Sell)")
                        closed = self.close_partial_short(symbol, size)
                        if closed:
                            result['short_closed'] = True
                        else:
                            result['errors'].append("Fehler beim Schließen der Short-Position")
            
            result['success'] = result['long_closed'] or result['short_closed']
            return result
            
        except Exception as e:
            logger.error(f"Fehler beim Schließen der Positionen für {symbol}: {e}", exc_info=True)
            result['errors'].append(str(e))
            return result

    def open_short_stop_order(self, symbol, trigger_price, size):
        """
        Öffnet eine Short-Stop-Order (für Short-Reentry nach TP).
        Wird ausgelöst, wenn der Preis auf oder unter trigger_price fällt.
        """
        try:
            # Hole Symbol-Info für korrekte Quantity-Rundung
            qty_step, min_qty = self.get_symbol_info(symbol)
            if qty_step:
                # Runde Size auf nächstes Vielfaches von qtyStep
                raw_size = float(size)
                rounded_size = round(raw_size / qty_step) * qty_step
                if rounded_size < min_qty:
                    rounded_size = min_qty
                # Stelle sicher, dass keine überflüssigen Dezimalstellen vorhanden sind
                decimals = len(str(qty_step).split('.')[-1]) if '.' in str(qty_step) else 0
                size = round(rounded_size, decimals)
            else:
                size = round(float(size), 2)
            
            logger.info(f"Öffne Short-Stop-Order: Symbol={symbol}, Trigger={trigger_price}, Size={size}")
            
            url = f'{self.base_url}/v5/order/create'
            recv_window = '20000'  # Erhöht für VPN-Latenz
            content_type = 'application/json'
            
            # Bybit Stop-Order Request Body
            # Für Sell-Stop: triggerDirection 2 (up) - umgekehrte Logik bei Bybit
            request_body = {
                "category": "linear",
                "symbol": symbol,
                "side": "Sell",
                "orderType": "Market",
                "qty": str(float(size)),
                "triggerPrice": str(trigger_price),
                "triggerBy": "LastPrice",
                "triggerDirection": 2,  # 2 = up für Sell-Stop (Bybit-Logik)
                "positionIdx": 2,  # 2 = Sell-Side (für Hedge-Mode)
                "timeInForce": "IOC",
                "reduceOnly": False,
                "closeOnTrigger": False
            }
            
            request_body_json = json.dumps(request_body)
            timestamp = str(int(time.time() * 1000))
            params_to_sign = timestamp + self.api_key + recv_window + request_body_json
            signature = hmac.new(
                self.secret_key.encode('utf-8'), params_to_sign.encode('utf-8'), hashlib.sha256
            ).hexdigest()
            
            headers = {
                'Content-Type': content_type,
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-RECV-WINDOW': recv_window,
                'X-BAPI-SIGN': signature,
                'X-BAPI-TIMESTAMP': timestamp
            }
            
            response = requests.post(url, headers=headers, data=request_body_json)
            
            if response.status_code == 200:
                response_json = response.json()
                if response_json.get("retCode") == 0:
                    order_id = response_json.get("result", {}).get("orderId", "N/A")
                    logger.info(f"Short-Stop-Order erfolgreich gesetzt: OrderId={order_id}")
                    return response_json
                else:
                    logger.error(f"Fehler beim Setzen der Short-Stop-Order: {response_json.get('retMsg')} (retCode: {response_json.get('retCode')})")
                    return None
            else:
                logger.error(f"HTTP-Fehler beim Setzen der Short-Stop-Order: {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"Fehler beim Öffnen der Short-Stop-Order: {e}", exc_info=True)
            return None

    def open_long_stop_order(self, symbol, trigger_price, size):
        """
        Öffnet eine Long-Stop-Order (für Long-Reentry nach TP).
        Wird ausgelöst, wenn der Preis auf oder über trigger_price steigt.
        """
        try:
            # Hole Symbol-Info für korrekte Quantity-Rundung
            qty_step, min_qty = self.get_symbol_info(symbol)
            if qty_step:
                # Runde Size auf nächstes Vielfaches von qtyStep
                raw_size = float(size)
                rounded_size = round(raw_size / qty_step) * qty_step
                if rounded_size < min_qty:
                    rounded_size = min_qty
                # Stelle sicher, dass keine überflüssigen Dezimalstellen vorhanden sind
                decimals = len(str(qty_step).split('.')[-1]) if '.' in str(qty_step) else 0
                size = round(rounded_size, decimals)
            else:
                size = round(float(size), 2)
            
            logger.info(f"Öffne Long-Stop-Order: Symbol={symbol}, Trigger={trigger_price}, Size={size}")
            
            url = f'{self.base_url}/v5/order/create'
            recv_window = '20000'  # Erhöht für VPN-Latenz
            content_type = 'application/json'
            
            # Bybit Stop-Order Request Body
            # Für Buy-Stop: triggerDirection 1 (down) - umgekehrte Logik bei Bybit
            request_body = {
                "category": "linear",
                "symbol": symbol,
                "side": "Buy",
                "orderType": "Market",
                "qty": str(float(size)),
                "triggerPrice": str(trigger_price),
                "triggerBy": "LastPrice",
                "triggerDirection": 1,  # 1 = down für Buy-Stop (Bybit-Logik)
                "positionIdx": 1,  # 1 = Buy-Side (für Hedge-Mode)
                "timeInForce": "IOC",
                "reduceOnly": False,
                "closeOnTrigger": False
            }
            
            request_body_json = json.dumps(request_body)
            timestamp = str(int(time.time() * 1000))
            params_to_sign = timestamp + self.api_key + recv_window + request_body_json
            signature = hmac.new(
                self.secret_key.encode('utf-8'), params_to_sign.encode('utf-8'), hashlib.sha256
            ).hexdigest()
            
            headers = {
                'Content-Type': content_type,
                'X-BAPI-API-KEY': self.api_key,
                'X-BAPI-RECV-WINDOW': recv_window,
                'X-BAPI-SIGN': signature,
                'X-BAPI-TIMESTAMP': timestamp
            }
            
            response = requests.post(url, headers=headers, data=request_body_json)
            
            if response.status_code == 200:
                response_json = response.json()
                if response_json.get("retCode") == 0:
                    order_id = response_json.get("result", {}).get("orderId", "N/A")
                    logger.info(f"Long-Stop-Order erfolgreich gesetzt: OrderId={order_id}")
                    return response_json
                else:
                    logger.error(f"Fehler beim Setzen der Long-Stop-Order: {response_json.get('retMsg')} (retCode: {response_json.get('retCode')})")
                    return None
            else:
                logger.error(f"HTTP-Fehler beim Setzen der Long-Stop-Order: {response.status_code}")
                return None
                
        except Exception as e:
            logger.error(f"Fehler beim Öffnen der Long-Stop-Order: {e}", exc_info=True)
            return None