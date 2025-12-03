import tkinter as tk
import threading
import time
import pandas as pd
import os
import sys
import datetime
import ta.volatility
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed

# Kendi modüllerimiz
sys.path.append(os.path.dirname(os.path.abspath(__file__))) 

from config import (TESTNET_API_KEY, TESTNET_SECRET_KEY, TESTNET_BASE_URL, 
                    REAL_API_KEY, REAL_SECRET_KEY, REAL_BASE_URL)
from gui import BotGUI 
from strategies.score import SignalEngine
from strategy import StrategyCore 
from binance.um_futures import UMFutures
from binance.error import ClientError

# --- YENİ EKLEME: Risk Modülü ---
from strategies.risk import calculate_dynamic_stops
from strategies.risk import (
    calc_chandelier_exit,
    calc_swing_exit,
    calc_msb_exit
)
# --------------------------------

# LOGLARI SUSTURMA
import logging
logging.getLogger("urllib3").setLevel(logging.ERROR)

UPDATE_INTERVAL_SECONDS = 5 
SCAN_INTERVAL_SECONDS = 120 

class AllyGatorLogic:
    def __init__(self, root):
        self.root = root
        #self.log = log_func if log_func else print
        
        # Durum Değişkenleri
        self.is_running = False        
        self.trading_active = False    
        self.scan_thread_active = False # YENİ EKLEME
        self.client: UMFutures = None  
        self.strategy_core: StrategyCore = None
        
        # Hesap İstatistikleri
        self.current_balance = 0.0
        self.start_balance = 0.0
        self.bot_realized_pnl = 0.0
        self.winning_trades = 0
        self.losing_trades = 0
        self.total_position_value = 0.0 
        self.total_trades_count = 0
        
        self.start_time = None
        self.previous_symbols = set()   
        self.touched_symbols = set()    
        self.trailing_peaks = {}        

        # GUI Başlatma
        self.gui = BotGUI(
            root,
            start_callback=self.start_bot_monitor,      
            stop_callback=self.stop_bot_monitor,        
            toggle_trading_callback=self.toggle_trading,
            close_all_callback=self.close_all_positions,
            emergency_stop_callback=self.emergency_stop
        )
        
        self.gui.log("AllyGator v0.2 başlatıldı. (Modüler)", force=True)
        self.trading_active = self.gui.trading_active
        self.position_monitor_active = False

    # --- 1. GUI ETKİLEŞİMİ ---

    def start_bot_monitor(self):
        if self.is_running: return

        is_testnet = self.gui.mode_var.get() == 1
        if is_testnet:
            api_key = TESTNET_API_KEY
            secret_key = TESTNET_SECRET_KEY
            base_url = TESTNET_BASE_URL
        else:
            api_key = REAL_API_KEY
            secret_key = REAL_SECRET_KEY
            base_url = REAL_BASE_URL

        if not api_key or not secret_key:
            self.gui.log("HATA: API Key yok!", force=True)
            self.gui.on_stop_press()
            return
        
        try:
            self.client = UMFutures(key=api_key, secret=secret_key, base_url=base_url)
            self.gui.log("✅ Bağlantı başarılı.", force=True)
        except Exception as e:
            self.gui.log(f"❌ Bağlantı Hatası: {e}", force=True)
            self.gui.on_stop_press()
            return
        
        self.strategy_core = StrategyCore(self.client, self.gui.settings, self.gui.log)
        self.is_running = True
        self.start_time = time.time()
        self.position_monitor_active = True
        self.gui.log("Bot izleme döngüsü başlatıldı.", force=True)
        
        
        self.monitor_thread = threading.Thread(target=self.monitor_loop, daemon=True)
        self.monitor_thread.start()

        self.get_account_info()
        self.start_balance = self.current_balance
        self.gui.log(f"💰 Başlangıç Bakiyesi Kaydedildi: {self.start_balance:.2f} USDT", force=True)
        
        self.root.after(1000, self.update_gui_stats)
        
    def stop_bot_monitor(self):
        self.is_running = False
        self.trading_active = False
        self.position_monitor_active = False

    def toggle_trading(self, active_state):
        self.trading_active = active_state
        self.gui.trading_active = active_state
        
        if self.trading_active:
            if self.scan_thread_active: # Eğer zaten çalışıyorsa, yeni thread başlatma
                self.gui.log("⚠️ Tarama zaten aktif. Yeniden başlatılmıyor.", force=False)
                return

            self.scan_thread_active = False # Yeni thread başlayacak
            self.gui.log("ALIM MODU AKTİF. Coin taraması başlatılıyor...", force=True)
            threading.Thread(target=self.scan_and_trade_loop, daemon=True).start()
        else:
            self.scan_thread_active = False # Alım kapandı, thread'i durdurmaya izin ver
            self.gui.log("ALIM MODU PASİF. Sinyal işleme durduruldu.", force=True)

    def emergency_stop(self):
        self.stop_bot_monitor()
        self.close_all_positions()

    # --- 2. ANA DÖNGÜLER ---

    def monitor_loop(self):
        """TEK BİRLEŞTİRİLMİŞ MONITOR LOOP - Tüm pozisyon izlemeleri burada"""
        while self.is_running and self.position_monitor_active:
            try:
                # 1. Hesap bilgilerini güncelle
                self.get_account_info()
                
                # 2. Pozisyonları al
                positions = self.client.get_position_risk()
                current_positions = [p for p in positions if float(p.get('positionAmt', 0)) != 0]
                current_symbols = {p['symbol'] for p in current_positions}

                # 3. Kapanan pozisyonları tespit et ve temizle
                closed_symbols = self.previous_symbols - current_symbols
                for symbol in closed_symbols:
                    self.gui.log(f"🔚 {symbol} pozisyonu kapandı → temizlik yapılıyor...", force=True)
                    self.fetch_and_update_pnl(symbol)
                    self.safe_cancel_all_orders(symbol, reason="Pozisyon kapandı")
                    self.trailing_peaks.pop(symbol, None)

                self.previous_symbols = current_symbols
                
                # 4. AÇIK POZİSYONLARI İZLE - ATR TRAILING STOP
                for pos in current_positions:
                    symbol = pos['symbol']
                    
                    # ATR Trailing Stop'u güncelle
                    self.update_trailing_stop(symbol, pos)
                    
                    # Ek risk kontrolü (isteğe bağlı)
                    self.additional_risk_checks(pos)
                
                # 5. GUI'yi güncelle
                self.update_open_positions()

            except Exception as e:
                self.gui.log(f"Monitor loop hatası: {e}", force=False)
            
            time.sleep(4)  # 4 saniyede bir kontrol

    def additional_risk_checks(self, position):
        """Ek risk kontrolleri - zarar kesme vs."""
        try:
            symbol = position['symbol']
            unrealized_pnl = float(position['unRealizedProfit'])
            
            # %10'dan fazla zararda kes (acil durum)
            if unrealized_pnl < -(self.current_balance * 0.10):
                self.gui.log(f"⛔ ACİL ZARAR KES: {symbol} | Kayıp: {unrealized_pnl:.2f} USDT", force=True)
                self.close_single_position(symbol)
                
        except Exception as e:
            self.gui.log(f"Risk check hatası: {e}", force=False)

    def update_trailing_stop(self, symbol, position_data):
        """GÜNCELLENMİŞ ATR Trailing Stop - Mevcut terminoloji korundu"""
        
        if symbol not in self.trailing_peaks:
            return

        try:
            current_price = float(position_data['markPrice'])
            position_amt = float(position_data['positionAmt'])

            if position_amt == 0:
                self.trailing_peaks.pop(symbol, None)
                return

            trail_data = self.trailing_peaks[symbol]
            direction = trail_data['direction']
            atr = trail_data['atr']
            entry_price = trail_data['entry_price']
            initial_sl = trail_data['initial_stop_loss']

            # 🚫 ATR KONTROLÜ - Eğer 0 ise düzelt
            if atr == 0:
                atr = current_price * 0.01  # Fiyatın %1'i
                trail_data['atr'] = atr
                self.gui.log(f"🔄 {symbol}: ATR 0'dı, düzeltildi: {atr:.6f}")

            # KAR HESABI
            if direction == "LONG":
                current_profit = current_price - entry_price
                profit_pct = (current_profit / entry_price) * 100
            else:  # SHORT
                current_profit = entry_price - current_price  
                profit_pct = (current_profit / entry_price) * 100

            # 🔥 MINIMUM KAR KONTROLÜ - Sadece %1'den (arayüden giriş)fazla karda çalış
            if profit_pct < self.gui.settings['tp_pct']:
                return

            # 🔥 PEAK/DIP GÜNCELLEME
            if direction == "LONG":
                # Yeni zirve kontrolü
                if current_price > trail_data['peak_price']:
                    trail_data['peak_price'] = current_price
                    # self.gui.log(f"📈 {symbol} Yeni Zirve: {current_price:.6f}")

                # Çıkış fiyatı: Zirve - (ATR × Multiplier)
                multiplier = trail_data.get('multiplier', 1.8)
                exit_trigger = trail_data['peak_price'] - (atr * multiplier)

                # TP tetikleme
                if current_price <= exit_trigger:
                    self.gui.log(
                        f"🎯 ATR ÇIKIŞ: {symbol} | Kar: {profit_pct:.2f}% | "
                        f"Zirve: {trail_data['peak_price']:.6f} → Çıkış: {current_price:.6f}",
                        force=True
                    )
                    self.close_single_position(symbol)
                    return

            else:  # SHORT
                # Yeni dip kontrolü
                if current_price < trail_data['peak_price']:
                    trail_data['peak_price'] = current_price
                    # self.gui.log(f"📉 {symbol} Yeni Dip: {current_price:.6f}")

                # Çıkış fiyatı: Dip + (ATR × Multiplier)
                multiplier = trail_data.get('multiplier', 1.8)
                exit_trigger = trail_data['peak_price'] + (atr * multiplier)

                if current_price >= exit_trigger:
                    self.gui.log(
                        f"🎯 ATR ÇIKIŞ: {symbol} | Kar: {profit_pct:.2f}% | "
                        f"Dip: {trail_data['peak_price']:.6f} → Çıkış: {current_price:.6f}",
                        force=True
                    )
                    self.close_single_position(symbol)
                    return
            df_recent = self.strategy_core.get_candlesticks(symbol, interval='15m', limit=100)

            
            # 1) Chandelier Exit
            ce_price = calc_chandelier_exit(df_recent, direction, atr)
            if ce_price:
                if direction == "LONG" and current_price <= ce_price:
                    self.gui.log(f"🎯 CE Exit: {symbol} | {current_price} (CE:{ce_price})", force=True)
                    self.close_single_position(symbol)
                    return
                elif direction == "SHORT" and current_price >= ce_price:
                    self.gui.log(f"🎯 CE Exit: {symbol} | {current_price} (CE:{ce_price})", force=True)
                    self.close_single_position(symbol)
                    return

            # 2) Swing Exit
            swing_level = calc_swing_exit(df_recent, direction)
            if swing_level:
                if direction == "LONG" and current_price < swing_level:
                    self.gui.log(f"📉 Swing Exit: {symbol}", force=True)
                    self.close_single_position(symbol)
                    return
                elif direction == "SHORT" and current_price > swing_level:
                    self.gui.log(f"📈 Swing Exit: {symbol}", force=True)
                    self.close_single_position(symbol)
                    return

            # 3) MSB Exit
            msb_level = calc_msb_exit(df_recent, direction)
            if msb_level:
                if direction == "LONG" and current_price < msb_level:
                    self.gui.log(f"💥 MSB Exit: {symbol}", force=True)
                    self.close_single_position(symbol)
                    return
                elif direction == "SHORT" and current_price > msb_level:
                    self.gui.log(f"💥 MSB Exit: {symbol}", force=True)
                    self.close_single_position(symbol)
                    return
        except Exception as e:
            self.gui.log(f"❌ Trailing stop hatası ({symbol}): {e}", force=False)

    


    def scan_and_trade_loop(self):
        if not self.strategy_core: return
        
        while self.is_running and self.trading_active:
            try:
                current_settings = self.gui.get_latest_settings_from_ui()
                self.strategy_core.settings = current_settings.copy()
                self.strategy_core.engine.settings = current_settings.copy()  # Engine'i doğrudan güncelle
                self.gui.settings = current_settings

                symbols = self.strategy_core.get_symbols_to_scan()
                
                if not symbols:
                    self.gui.log("UYARI: Hiçbir sembol filtreyi geçemedi.", force=True)
                    continue
                    
                # DÜZGÜN ThreadPool kullanımı
                with ThreadPoolExecutor(max_workers=5) as executor:
                    # Tüm sembolleri paralel işle
                    futures = {}
                    for symbol in symbols:
                        if not self.is_running or not self.trading_active:
                            break
                        # Her sembol için analiz işlemini başlat
                        future = executor.submit(self.analyze_and_trade_symbol, symbol)
                        futures[future] = symbol
                    
                    # Sonuçları bekle ve işle
                    for future in as_completed(futures):
                        if not self.is_running or not self.trading_active:
                            break
                        symbol = futures[future]
                        try:
                            result = future.result()
                            # result burada sinyal bilgisi olacak
                        except Exception as e:
                            self.gui.log(f"❌ {symbol} analiz hatası: {e}", force=False)
                    
            except Exception as e:
                self.gui.log(f"❌ Tarama Hatası: {e}", force=True)

            if not self.is_running or not self.trading_active:
                break
                
            self.gui.log(f"Tarama bitti. {SCAN_INTERVAL_SECONDS} sn bekleme...", force=True)
            time.sleep(SCAN_INTERVAL_SECONDS)

            self.scan_thread_active = False
        
        if self.trading_active and not self.is_running:
                    self.gui.log("Sistem durduruldu, tarama bitti.", force=True)
        elif self.is_running and not self.trading_active:
                    self.gui.log("ALIM MODU kapandı, tarama bitti.", force=True)

    # YENİ FONKSİYON EKLE - Thread'lerde çalışacak
    def analyze_and_trade_symbol(self, symbol):
        """Tek bir sembolü analiz eder ve trade açar"""
        try:
            #self.gui.log(f"-> {symbol} kontrol...", force=False)
            df = self.strategy_core.get_candlesticks(symbol, interval='15m', limit=100)
            if df is None: 
                return None
                
            leverage, avg_vol = self.strategy_core.calculate_volatility(df)
            
            # Güncellenmiş sinyal çağrısı
            signal, reason, _, _, _ = self.strategy_core.generate_signal(df, symbol=symbol)
            
            if signal != "HOLD":
                self.gui.log(f"🔔 SİNYAL: {symbol} -> {signal} | Kaldıraç: {leverage}x | Vol: {avg_vol:.2f}% | Sebep: {reason}", force=True)
                
                if not self.has_open_position(symbol):
                    self.open_position(symbol, signal, leverage, df)
                else:
                    self.gui.log(f"⚠️ {symbol} zaten açık pozisyon var.", force=False)
                    
            return signal
            
        except Exception as e:
            self.gui.log(f"❌ {symbol} işlem hatası: {e}", force=False)
            return None

    # --- 3. EMİR VE POZİSYON YÖNETİMİ ---

    def has_open_position(self, symbol):
        try:
            positions = self.client.get_position_risk(symbol=symbol)
            for p in positions:
                if float(p['positionAmt']) != 0:
                    return True
            return False
        except:
            return False

    def set_leverage_and_margin_mode(self, symbol, leverage):
        margin_mode = "ISOLATED" if self.gui.isolated_var.get() == 1 else "CROSSED"
        try:
            self.client.change_margin_type(symbol=symbol, marginType=margin_mode)
        except ClientError: pass
        
        try:
            self.client.change_leverage(symbol=symbol, leverage=leverage)
            return True 
        except ClientError as ce:
             self.gui.log(f"❌ Kaldıraç Hatası ({symbol}): {ce.error_message}", force=True)
             return True

    def round_step_size(self, quantity, step_size):
        """Basit ve güvenli rounding"""
        return round(quantity, 6)  # Çoğu coin için yeterli

    def round_price(self, price, tick_size):
        """Basit ve güvenli rounding"""
        return round(price, 8)  # Çoğu coin için yeterli
    
    def clean_open_orders(self, symbol):
        try:
            self.client.cancel_open_orders(symbol=symbol)
        except Exception: pass
    
    def safe_cancel_all_orders(self, symbol, reason="Temizlik"):
        try:
            if hasattr(self.client, 'futures_cancel_open_orders'):
                self.client.futures_cancel_open_orders(symbol=symbol)
            else:
                self.client.cancel_open_orders(symbol=symbol)
                self.gui.log(f"{symbol} → Tüm açık emirler iptal edildi ({reason})", force=True)
        except Exception: pass

    def fetch_and_update_pnl(self, symbol):
        """Kapanan pozisyonun sonucunu Binance geçmişinden çeker."""
        try:
            trades = self.client.get_account_trades(symbol=symbol, limit=1)
            if trades:
                last_trade = trades[0]
                pnl = float(last_trade.get('realizedPnl', 0))
                if abs(pnl) > 0:
                    self.bot_realized_pnl += pnl
                    if pnl > 0: self.winning_trades += 1
                    else: self.losing_trades += 1
                    self.gui.log(f"📝 {symbol} Geçmişten PNL İşlendi: {pnl:.2f} USDT", force=True)
                    self.root.after(0, self.update_gui_stats)
        except Exception as e:
            self.gui.log(f"PNL Geçmiş Hatası ({symbol}): {e}", force=False)

    # --- KRİTİK DEĞİŞİKLİK: open_position artık 'df' alıyor ---
    def calculate_atr(self, df, period=14):
        """
        Average True Range (ATR) hesaplar - GÜVENLİ VERSİYON
        """
        try:
            if len(df) < period + 1:
                return 0.0
                
            atr_indicator = ta.volatility.AverageTrueRange(
                high=df['High'], low=df['Low'], close=df['Close'], window=period
            )
            atr_series = atr_indicator.average_true_range()
            
            atr_value = atr_series.iloc[-1] if not atr_series.empty else 0.0
            
            if atr_value == 0 or pd.isna(atr_value):
                current_price = df['Close'].iloc[-1]
                atr_value = current_price * 0.01
                
            return atr_value
            
        except Exception as e:
            current_price = df['Close'].iloc[-1]
            return current_price * 0.01

    def open_position(self, symbol, signal, leverage, df):
        if self.has_open_position(symbol):
            return

        try:
            account_resp = self.client.account()
            available_balance = 0.0
            for asset in account_resp['assets']:
                if asset['asset'] == 'USDT':
                    available_balance = float(asset['walletBalance']) # KONTROL ET
                    break
        except:
            available_balance = self.current_balance 

        budget_pct = self.gui.settings['budget_pct'] / 100 
        investment_amount = available_balance * budget_pct 
        max_risk_per_trade = available_balance * 0.02
        investment_amount = min(investment_amount, max_risk_per_trade)
        
        if investment_amount < 6: 
            self.gui.log(f"⚠️ {symbol}: Yetersiz Bakiye ({investment_amount:.2f}).", force=True)
            return

        self.set_leverage_and_margin_mode(symbol, leverage)

        try:
            ticker = self.client.ticker_price(symbol)
            current_price = float(ticker['price'])
            
            exchange_info = self.client.exchange_info()
            step_size = 0.001
            tick_size = 0.01
            for s in exchange_info['symbols']:
                if s['symbol'] == symbol:
                    for f in s['filters']:
                        if f['filterType'] == 'LOT_SIZE': 
                            step_size = float(f['stepSize'])
                        if f['filterType'] == 'PRICE_FILTER': 
                            tick_size = float(f['tickSize'])
                    break
            
            qty_raw = (investment_amount * leverage) / current_price 
            
            if step_size <= 0:
                step_size = 0.001

            try:
                qty_precision = max(0, min(6, int(round(-np.log10(step_size), 0))))
            except:
                qty_precision = 3

            quantity = float(f"{qty_raw:.{qty_precision}f}")
            
            if quantity == 0: return

            # ATR HESAPLAMA - Risk.py'deki calculate_atr fonksiyonunu kullan
            #from risk import calculate_atr  # Risk dosyasından import et
            atr_val = self.calculate_atr(df)
    
            # ATR çok küçükse minimum değer kullan
            current_price = float(ticker['price'])
            min_atr = current_price * 0.005  # Minimum %0.5
            if atr_val < min_atr:
                atr_val = min_atr
                self.gui.log(f"⚠️ {symbol}: ATR çok küçük, min. değer kullanılıyor: {atr_val:.6f}")
            
            if signal == "LONG":
                order_side = "BUY"
                close_side = "SELL"
            else: 
                order_side = "SELL"
                close_side = "BUY"
            
            if tick_size <= 0:
                tick_size = 0.01

            try:
                price_precision = max(2, min(8, int(round(-np.log10(tick_size), 0))))
            except:
                price_precision = 4

            # SABİT STOP LOSS (Arayüzden gelen sl_pct ile)
            sl_pct = self.gui.settings['sl_pct'] / 100
            if signal == "LONG":
                stop_loss_fixed = current_price * (1 - sl_pct)
            else:  # SHORT
                stop_loss_fixed = current_price * (1 + sl_pct)
            
            stop_loss_fixed = round(stop_loss_fixed, price_precision)

            try:
                # Pozisyon aç
                self.client.new_order(symbol=symbol, side=order_side, type="MARKET", quantity=quantity)
                self.gui.log(f"🚀 {symbol} {signal} AÇILDI. {quantity} adet @ {current_price} | ATR: {atr_val:.4f}", force=True)
                
                # Stop loss emri (SABİT - değişmedi)
                if signal == "LONG":
                    self.client.new_order(
                        symbol=symbol, 
                        side="SELL", 
                        type="STOP_MARKET", 
                        stopPrice=stop_loss_fixed, 
                        closePosition="true"
                    )
                else:  # SHORT
                    self.client.new_order(
                        symbol=symbol, 
                        side="BUY", 
                        type="STOP_MARKET", 
                        stopPrice=stop_loss_fixed, 
                        closePosition="true"
                    )
                
                self.gui.log(f"🛡️ {symbol} STOP LOSS: {stop_loss_fixed}", force=True)
                
                # ATR'yi trailing stop için kaydet - GÜNCELLENMİŞ
                self.trailing_peaks[symbol] = {
                    'atr': atr_val,
                    'direction': signal,
                    'entry_price': current_price,
                    'peak_price': current_price,  # LONG için entry, SHORT için entry
                    'initial_stop_loss': stop_loss_fixed,  # Sabit SL'i de kaydet
                    'multiplier': 1.8  # Varsayılan multiplier
                }
    
                self.gui.log(f"🎯 {symbol} Trailing Start: ATR={atr_val:.6f}, SL={stop_loss_fixed:.6f}")
                
            except Exception as sl_error:
                self.gui.log(f"❌ {symbol} stop loss hatası: {sl_error}", force=True)
            
            self.touched_symbols.add(symbol)
            self.total_trades_count += 1
            
        except Exception as e:
            self.gui.log(f"❌ İşlem Hatası ({symbol}): {e}", force=True)
    def close_all_positions(self):
        """Tümünü kapatır - Geliştirilmiş versiyon"""
        self.gui.log("⚠️ TÜM POZİSYONLAR İÇİN KAPATMA EMRİ VERİLİYOR...", force=True)
        try:
            all_positions = self.client.get_position_risk()
            active_positions = [p for p in all_positions if float(p['positionAmt']) != 0]

            if not active_positions:
                self.gui.log("⚠️ Kapatılacak açık pozisyon yok.", force=True)
                return

            self.gui.log(f"🚀 {len(active_positions)} adet pozisyon sırayla kapatılıyor...", force=True)
            
            success_count = 0
            error_count = 0
            
            # Her pozisyon için sırayla işlem yap (paralel değil)
            for i, pos in enumerate(active_positions):
                symbol = pos['symbol']
                try:
                    self.gui.log(f"📦 {i+1}/{len(active_positions)} {symbol} kapatılıyor...", force=False)
                    self.close_single_position(symbol)
                    success_count += 1
                    
                    # Her 5 işlemde bir 1 saniye bekle (rate limit koruması)
                    if (i + 1) % 5 == 0:
                        time.sleep(1)
                        
                except Exception as e:
                    error_count += 1
                    self.gui.log(f"❌ {symbol} kapatma hatası: {e}", force=True)
                    continue

            self.gui.log(f"✅ {success_count} pozisyon başarıyla kapatıldı, {error_count} hata", force=True)

            time.sleep(0.5)
            #self.previous_symbols.clear()    -- ekran temizleme
            self.root.after(0, self.update_open_positions)
        except Exception as e:
            self.gui.log(f"❌ Toplu Kapatma Hatası: {e}", force=True)

    def get_account_info(self):
        if not self.client: return
        try:
            res = self.client.account()
            for asset in res['assets']:
                if asset['asset'] == 'USDT':
                    self.current_balance = float(asset['walletBalance'])
                    break
            
            if self.start_balance > 0:
                self.bot_realized_pnl = self.current_balance - self.start_balance
            else:
                self.bot_realized_pnl = 0.0

        except Exception as e:
            pass

    def update_open_positions(self):
        if not self.client: return
        try:
            positions = self.client.get_position_risk()
            active_positions = [p for p in positions if float(p.get('positionAmt', 0)) != 0]
            self.total_position_value = sum(float(p['markPrice']) * abs(float(p['positionAmt'])) for p in active_positions)

            def update_gui():
                for widget in self.gui.scrollable_frame.winfo_children():
                    widget.destroy()

                if not active_positions:
                    tk.Label(self.gui.scrollable_frame, text="Açık Pozisyon Bulunmuyor", bg="#2C3E50", fg="#BDC3C7", font=("Arial", 12, "bold")).pack(pady=20, anchor="center")
                    return

                for i, pos in enumerate(active_positions):
                    symbol = pos['symbol']
                    position_amt = float(pos['positionAmt'])
                    side = "LONG" if position_amt > 0 else "SHORT"
                    amount = abs(position_amt)
                    entry_price = float(pos['entryPrice'])
                    unrealized_pnl = float(pos['unRealizedProfit'])
                    #liq_price = float(pos.get('liquidationPrice', 0)) or 0.0
                    leverage = pos.get('leverage', 'N/A')

                    pnl_color = "#2ECC71" if unrealized_pnl >= 0 else "#E74C3C"
                    row_color = "#34495E" if i % 2 == 0 else "#2C3E50"

                    row = tk.Frame(self.gui.scrollable_frame, bg=row_color, relief=tk.RIDGE, bd=1)
                    row.pack(fill=tk.X, pady=2, padx=5)

                    info_text = (f"{symbol:<12}|{side:<5} | Mik: {amount:>8.4f} | "
                                 f"Gir: ${entry_price:>8.2f} | PNL:${unrealized_pnl:>8.2f} | "
                                 f" | X: {leverage}")  # Liq: ${liq_price:>8.2f}  -- çıkardım
                    
                    tk.Label(row, text=info_text, font=("Consolas", 9), fg=pnl_color, bg=row_color, justify=tk.LEFT).pack(side=tk.LEFT, padx=10, pady=4)
                    
                    tk.Button(row, text="KAPAT", bg="#E74C3C", fg="white", font=("Arial", 8, "bold"),
                              command=lambda s=symbol: self.close_single_position(s)).pack(side=tk.RIGHT, padx=10, pady=4)

                if hasattr(self.gui, 'lbl_positions'):
                    self.gui.lbl_positions.config(text=f"{len(active_positions)}")

            self.root.after(0, update_gui)
        except Exception: pass

    def close_single_position(self, symbol):
        def _action():
            try:
                positions = self.client.get_position_risk(symbol=symbol)
                amt = 0.0
                entry_price = 0.0
                for p in positions:
                    if float(p['positionAmt']) != 0:
                        amt = float(p['positionAmt'])
                        entry_price = float(p['entryPrice'])
                        break
                
                if amt == 0:
                    #self.safe_cancel_all_orders(symbol, reason="Temizlik")
                    self.update_open_positions()
                    return

                side = "SELL" if amt > 0 else "BUY"
                qty = abs(amt)
                
                response = self.client.new_order(symbol=symbol, side=side, type="MARKET", quantity=qty, reduceOnly="true", recvWindow=30000)
                
                self.previous_symbols.discard(symbol) 
                #self.safe_cancel_all_orders(symbol)

                exit_price = float(response.get('avgPrice', 0))
                if exit_price == 0:
                    try:
                        ticker = self.client.ticker_price(symbol)
                        exit_price = float(ticker['price'])
                    except:
                        exit_price = entry_price

                if amt > 0: realized_pnl = (exit_price - entry_price) * qty
                else: realized_pnl = (entry_price - exit_price) * qty
                
                self.bot_realized_pnl += realized_pnl
                if realized_pnl > 0: self.winning_trades += 1
                else: self.losing_trades += 1
                    
                self.gui.log(f"✅ {symbol}... PNL: {realized_pnl:.2f} USDT", force=True)
                self.safe_cancel_all_orders(symbol, reason=f"temizlik")
                self.trailing_peaks.pop(symbol, None)

                self.root.after(0, self.update_gui_stats)
                time.sleep(1.0)
                self.update_open_positions()
                
            except Exception as e:
                self.gui.log(f"❌ Kapatma Hatası ({symbol}): {e}", force=True)
        
        threading.Thread(target=_action, daemon=True).start()

    def update_gui_stats(self):
        if not self.is_running: return
        try:
            if self.start_time:
                elapsed = int(time.time() - self.start_time)
                h, r = divmod(elapsed, 3600)
                m, s = divmod(r, 60)
                self.gui.lbl_runtime.config(text=f"{h:02d}:{m:02d}:{s:02d}")

            self.gui.lbl_balance.config(text=f"{self.current_balance:.2f} $")
            self.gui.lbl_bot_pnl.config(text=f"{self.bot_realized_pnl:+.2f} $")
            self.gui.lbl_trade_count.config(text=str(self.total_trades_count))
            self.gui.lbl_total_pos_value.config(text=f"{self.total_position_value:.2f} $")
            self.gui.lbl_stats.config(text=f"{self.winning_trades} / {self.losing_trades}")
            self.gui.last_update_label.config(text=f"Son Güncelleme: {datetime.datetime.now().strftime('%H:%M:%S')}")

        except Exception as e:
            print(f"GUI Stat Error: {e}")
        
        finally:
            self.root.after(1000, self.update_gui_stats)
        
if __name__ == "__main__":
    root = tk.Tk()
    app = AllyGatorLogic(root)
    def on_closing():
        if app.is_running: app.stop_bot_monitor()
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_closing)
    root.mainloop()
