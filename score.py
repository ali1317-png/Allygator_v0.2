import pandas as pd
import ta.momentum
import ta.volatility
import ta.trend
import numpy as np

# Kendi modüllerimizi dahil ediyoruz
from strategies.fvg import detect_fvg, check_fvg_signal, detect_fvg_fill
from strategies.structure import detect_structure, check_trend


class SignalEngine:
    def __init__(self, settings=None, log_func=None):
        # Log fonksiyonu varsa onu kullan, yoksa boş print yap
        self.settings = settings or {}   # GUI tarafından geçilecek
        self.log = log_func if log_func else print
        
        # Puan Ağırlıkları (İstediğin gibi değiştirebilirsin)
        self.weights = {
            'fvg': 4,              # FVG Ana Sinyal
            'structure': 3,        # Trend
            'rsi_boll': 2,         # Filtre
            'liquidity': 3,        # Likidite analizi
            'volume_profile': 2,   # Hacim profili
            'order_block': 3,      # Order block
            'order_flow': 3,       # CVD ve delta
            'pd_arrays': 2,        # Premium/Discount
            'ote': 3,              # Fibonacci OTE
            'killzones': 1,        # Zaman bazlı
            'ml': 2,               # Makine öğrenmesi
            'threshold':20     # Minimum puan (arayüzden)
        }
    @property
    def threshold(self):
        # settings içinde yoksa weights'den al
        try:
            return int(self.settings.get('score_thresh', self.weights['threshold']))
        except Exception as e:
            self.log(f"SignalEngine.threshold okuma hatası: {e} → varsayılan döndürülüyor")
            return int(self.weights['threshold'])

    def update_settings(self, new_settings):
        """Main thread GUI güncellendiğinde bu metodu çağır."""

        if not isinstance(new_settings, dict):
            self.log(f"update_settings: new_settings dict değil ({type(new_settings)}) - gözardı ediliyor.")
            return

        self.settings = new_settings
    def calculate_indicators(self, df):
        """Yardımcı indikatörleri hesapla (RSI, Bollinger)"""
        df['rsi'] = ta.momentum.rsi(df['Close'], window=14)
        bb = ta.volatility.BollingerBands(df['Close'], window=20, window_dev=2)
        df['bb_upper'] = bb.bollinger_hband()
        df['bb_lower'] = bb.bollinger_lband()
        return df
    
    def get_higher_timeframe_data(self, symbol, client, interval='1d', limit=100):
        """Yüksek timeframe verisi çeker"""
        try:
            klines = client.klines(symbol, interval, limit=limit)
            df = pd.DataFrame(klines, columns=[
                'Open time', 'Open', 'High', 'Low', 'Close', 'Volume', 
                'Close time', 'Quote asset volume', 'Number of trades', 
                'Taker buy base asset volume', 'Taker buy quote asset volume', 'Ignore'
            ])
            cols = ['Open', 'High', 'Low', 'Close', 'Volume']
            df[cols] = df[cols].astype(float)
            return self.calculate_indicators(df)
        except Exception as e:
            self.log(f"Yüksek TF veri hatası ({symbol}): {e}")
            return None

    # --- MODÜL 1: Trend Analizi ---
    def _module_structure(self, df):
        l_score, s_score = 0, 0
        reason = ""
        
        # Lookback 5 (Senin istediğin gibi)
        df_struct = detect_structure(df, lookback=5)
        trend, is_bos = check_trend(df_struct)
        
        if trend == "BULLISH":
            l_score += self.weights['structure']
            if is_bos: 
                l_score += 1
                reason = "Trend Bullish + BOS"
            else:
                reason = "Trend Bullish"
                
        elif trend == "BEARISH":
            s_score += self.weights['structure']
            if is_bos: 
                s_score += 1
                reason = "Trend Bearish + BOS"
            else:
                reason = "Trend Bearish"
                
        return l_score, s_score, reason

    # --- MODÜL 2: FVG (Price Action) ---
    def _module_fvg(self, df):
        l_score, s_score = 0, 0
        reason = ""
        
        fvg_bull, fvg_bear = detect_fvg(df)
        fvg_bull = detect_fvg_fill(df, fvg_bull)
        fvg_bear = detect_fvg_fill(df, fvg_bear)
        
        # Sinyal kontrolü
        raw_signal = check_fvg_signal(df, fvg_bull, fvg_bear)
        
        if raw_signal > 0:
            l_score += self.weights['fvg']
            reason = "Bullish FVG Bölgesi"
        elif raw_signal < 0:
            s_score += self.weights['fvg']
            reason = "Bearish FVG Bölgesi"
            
        return l_score, s_score, reason

    # --- MODÜL 3: RSI & Bollinger (Filtre) ---
    def _module_rsi_bollinger(self, df):
        l_score, s_score = 0, 0
        reason = ""
        
        current_price = df['Close'].iloc[-1]
        rsi = df['rsi'].iloc[-1]
        bb_lower = df['bb_lower'].iloc[-1]
        bb_upper = df['bb_upper'].iloc[-1]
        
        # AND Mantığı: Hem RSI düşük olacak HEM Fiyat Bollinger altında olacak
        if rsi < 35 and current_price < bb_lower:
            l_score += self.weights['rsi_boll']
            reason = "RSI<35 & BB Altı"
            
        # AND Mantığı: Hem RSI yüksek olacak HEM Fiyat Bollinger üstünde olacak
        elif rsi > 70 and current_price > bb_upper:
            s_score += self.weights['rsi_boll']
            reason = "RSI>65 & BB Üstü"
            
        return l_score, s_score, reason
    
    # --- YENİ MODÜL 4: Likidite Analizi ---
    def _module_liquidity(self, df, window=20):
        l_score, s_score = 0, 0
        reason = ""
        
        try:
            # 1. Alt/Üst wick oranı analizi
            df['body'] = abs(df['Close'] - df['Open'])
            df['range'] = df['High'] - df['Low']
            df['lower_wick_ratio'] = (df['Open'] - df['Low']) / (df['range'] + 1e-8)
            df['upper_wick_ratio'] = (df['High'] - df['Open']) / (df['range'] + 1e-8)
            
            # Son 5 mumda strong lower wick + hacim spike
            current_lower_wick = df['lower_wick_ratio'].iloc[-1]
            current_volume = df['Volume'].iloc[-1]
            avg_volume = df['Volume'].tail(5).mean()
            
            if current_lower_wick > 0.6 and current_volume > avg_volume * 1.8:
                l_score += self.weights['liquidity']
                reason += "Lower Wick Hunt | "
            
            # Son 5 mumda strong upper wick + hacim spike  
            current_upper_wick = df['upper_wick_ratio'].iloc[-1]
            if current_upper_wick > 0.6 and current_volume > avg_volume * 1.8:
                s_score += self.weights['liquidity']
                reason += "Upper Wick Hunt | "
                
            # 2. Eşit düşükler/tepelere yakınlık (likidite bölgeleri)
            current_low = df['Low'].iloc[-1]
            current_high = df['High'].iloc[-1]
            
            # Son 50 mumda eşit düşükler
            recent_lows = df['Low'].tail(50)
            equal_lows = recent_lows[abs(recent_lows - recent_lows.shift(1)) < recent_lows * 0.001]
            if len(equal_lows) >= 2:
                liquidity_zone = equal_lows.min()
                if abs(current_low - liquidity_zone) / liquidity_zone < 0.002:  # %0.2 yakınsa
                    l_score += 2
                    reason += "Equal Lows Zone | "
                return l_score, s_score, reason
            
            # Son 50 mumda eşit yüksekler
            recent_highs = df['High'].tail(50)
            equal_highs = recent_highs[abs(recent_highs - recent_highs.shift(1)) < recent_highs * 0.001]
            if len(equal_highs) >= 2:
                liquidity_zone = equal_highs.max()
                if abs(current_high - liquidity_zone) / liquidity_zone < 0.002:
                    s_score += 2
                    reason += "Equal Highs Zone | "

                return l_score, s_score, reason
                    
        except Exception as e:
            self.log(f"Likidite modülü hatası: {e}")
    
    # --- YENİ MODÜL 5: Hacim Profili ve POC ---
    def _module_volume_profile(self, df, period=50):
        l_score, s_score = 0, 0
        reason = ""
        
        try:
            if len(df) < period:
                return l_score, s_score, "Yetersiz veri"
                
            # Basit hacim profili hesaplama
            recent_df = df.tail(period)
            price_min, price_max = recent_df['Low'].min(), recent_df['High'].max()
            
            # 20 fiyat seviyesine böl
            bins = np.linspace(price_min, price_max, 20)
            volume_profile = np.zeros(len(bins)-1)
            
            for i in range(len(recent_df)):
                close_price = recent_df['Close'].iloc[i]
                volume_val = recent_df['Volume'].iloc[i]
                
                # Hangi bine denk geliyor
                bin_idx = np.digitize(close_price, bins) - 1
                if 0 <= bin_idx < len(volume_profile):
                    volume_profile[bin_idx] += volume_val
            
            # POC (Point of Control) bul
            poc_idx = np.argmax(volume_profile)
            poc_price = bins[poc_idx]
            
            current_price = df['Close'].iloc[-1]
            
            # POC'a göre bias
            if current_price < poc_price * 0.99:  # POC'un %1 altında
                l_score += self.weights['volume_profile']
                reason += f"Price Below POC | "
            elif current_price > poc_price * 1.01:  # POC'un %1 üstünde
                s_score += self.weights['volume_profile']
                reason += f"Price Above POC | "
                
        except Exception as e:
            self.log(f"Hacim profili modülü hatası: {e}")
            
        return l_score, s_score, reason

    # --- YENİ MODÜL 6: Order Block Tespiti ---
    def _module_order_blocks(self, df, lookback=50):
        l_score, s_score = 0, 0
        reason = ""
        
        try:
            current_idx = len(df) - 1
            current_price = df['Close'].iloc[-1]
            
            # Son bearish mumdan önceki güçlü bullish mum (Bullish OB)
            for i in range(current_idx - 2, max(0, current_idx - lookback), -1):
                if (df['Close'].iloc[i-2] < df['Open'].iloc[i-2] and  # Önceki mum bearish
                    df['Close'].iloc[i-1] > df['Open'].iloc[i-1] and  # Şimdiki mum bullish
                    (df['Close'].iloc[i-1] - df['Open'].iloc[i-1]) > 2 * abs(df['Open'].iloc[i-2] - df['Close'].iloc[i-2])):
                    
                    ob_low = df['Low'].iloc[i-1]
                    ob_high = df['High'].iloc[i-1]
                    
                    # Fiyat bu bölgeye yakınsa
                    if ob_low <= current_price <= ob_high * 1.01:
                        l_score += self.weights['order_block']
                        reason += "Bullish OB Zone | "
                        break
            
            # Son bullish mumdan önceki güçlü bearish mum (Bearish OB)  
            for i in range(current_idx - 2, max(0, current_idx - lookback), -1):
                if (df['Close'].iloc[i-2] > df['Open'].iloc[i-2] and  # Önceki mum bullish
                    df['Close'].iloc[i-1] < df['Open'].iloc[i-1] and  # Şimdiki mum bearish
                    abs(df['Open'].iloc[i-1] - df['Close'].iloc[i-1]) > 2 * (df['Close'].iloc[i-2] - df['Open'].iloc[i-2])):
                    
                    ob_low = df['Low'].iloc[i-1]
                    ob_high = df['High'].iloc[i-1]
                    
                    # Fiyat bu bölgeye yakınsa
                    if ob_low * 0.99 <= current_price <= ob_high:
                        s_score += self.weights['order_block']
                        reason += "Bearish OB Zone | "
                        break
                        
        except Exception as e:
            self.log(f"Order block modülü hatası: {e}")
            
        return l_score, s_score, reason

    # --- YENİ MODÜL 7: PD Arrays (Premium/Discount) ---
    def _module_pd_arrays(self, df):
        l_score, s_score = 0, 0
        reason = ""
        
        try:
            if len(df) < 20:
                return l_score, s_score, "Yetersiz veri"
            
            # Basit PD Arrays implementasyonu
            weekly_high = df['High'].tail(100).max()
            weekly_low = df['Low'].tail(100).min()
            equilibrium = (weekly_high + weekly_low) / 2
            
            premium = weekly_high - (weekly_high - equilibrium) * 0.25
            discount = weekly_low + (equilibrium - weekly_low) * 0.25
            
            current_price = df['Close'].iloc[-1]
            
            if current_price < discount:
                l_score += self.weights['pd_arrays']
                reason += "Discount Zone | "
            elif current_price > premium:
                s_score += self.weights['pd_arrays'] 
                reason += "Premium Zone | "
                
        except Exception as e:
            self.log(f"PD Arrays modülü hatası: {e}")
            
        return l_score, s_score, reason

    # --- YENİ MODÜL 8: OTE (Optimal Trade Entry) Fibonacci ---
    def _module_ote(self, df, swing_period=30):
        l_score, s_score = 0, 0
        reason = ""
        
        try:
            if len(df) < swing_period + 5:
                return l_score, s_score, "Yetersiz veri"
            
            # Son swing high/low bul
            recent_high = df['High'].tail(swing_period).max()
            recent_low = df['Low'].tail(swing_period).min()
            
            fib_618 = recent_high - (recent_high - recent_low) * 0.618
            fib_786 = recent_high - (recent_high - recent_low) * 0.786
            
            current_price = df['Close'].iloc[-1]
            
            # Fiyat OTE bölgesinde mi (fib 0.618-0.786)
            if fib_786 <= current_price <= fib_618:
                # FVG ile kombine et (basit versiyon)
                fvg_bullish, fvg_bearish = detect_fvg(df)
                fvg_bullish = detect_fvg_fill(df, fvg_bullish)
                
                for fvg in fvg_bullish[-3:]:  # Son 3 FVG'yi kontrol et
                    if not fvg.get('filled', True) and abs(current_price - fvg['avg_price']) / current_price < 0.005:
                        l_score += self.weights['ote']
                        reason += "OTE + FVG | "
                        break
                        
        except Exception as e:
            self.log(f"OTE modülü hatası: {e}")
            
        return l_score, s_score, reason

    # --- YENİ MODÜL 9: Kill Zones (Zaman Bazlı) ---
    def _module_killzones(self):
        l_score, s_score = 0, 0
        reason = ""
        
        try:
            from datetime import datetime
            utc_hour = datetime.utcnow().hour
            
            # London Open (08:00-10:00 UTC) ve New York Open (13:30-16:00 UTC)
            if (8 <= utc_hour < 10) or (13.5 <= utc_hour < 16):
                l_score += self.weights['killzones']
                s_score += self.weights['killzones']  # Her iki yöne de puan
                reason += "Kill Zone Active | "
                
        except Exception as e:
            self.log(f"Kill zones modülü hatası: {e}")
            
        return l_score, s_score, reason


    # --- ANA PUANLAMA FONKSİYONU ---
    def get_composite_score(self, df, symbol=None, client=None):
        try:
            if len(df) < 50: return "HOLD", 0, "Yetersiz Veri"
            
            
            if not isinstance(self.settings, dict):
                try:
                    self.settings = dict(self.settings)  # Dönüştürmeye çalış
                except Exception:
                    self.settings = {}  # Başarısız olursa boş
                    self.log("DEBUG: settings could not be converted to dict → using empty")



            
            df = self.calculate_indicators(df)
        
            
            total_long_score = 0
            total_short_score = 0
            
            # Raporlama için detaylar
            reasons_log = []

            # --- PARALEL MODÜL ÇAĞRILARI ---
            # Burası tam istediğin modüler yapı. İleride 4. modülü buraya ekle yeter.
            
            # 1. Structure
            l1, s1, r1 = self._module_structure(df)
            total_long_score += l1
            total_short_score += s1
            if r1: reasons_log.append(f"[Yapı: {r1}]")
            
            # 2. FVG
            l2, s2, r2 = self._module_fvg(df)
            total_long_score += l2
            total_short_score += s2
            if r2: reasons_log.append(f"[FVG: {r2}]")
            
            # 3. RSI & Bollinger
            l3, s3, r3 = self._module_rsi_bollinger(df)
            total_long_score += l3
            total_short_score += s3
            if r3: reasons_log.append(f"[İndikatör: {r3}]")

            # YENİ MODÜLLER
            l4, s4, r4 = self._module_liquidity(df)
            total_long_score += l4
            total_short_score += s4
            if r4: reasons_log.append(f"[Likidite: {r4}]")

            l5, s5, r5 = self._module_volume_profile(df)
            total_long_score += l5
            total_short_score += s5
            if r5: reasons_log.append(f"[Hacim: {r5}]")

            l6, s6, r6 = self._module_order_blocks(df)
            total_long_score += l6
            total_short_score += s6
            if r6: reasons_log.append(f"[OB: {r6}]")

            l7, s7, r7 = self._module_pd_arrays(df)
            total_long_score += l7
            total_short_score += s7
            if r7: reasons_log.append(f"[PD: {r7}]")

            l8, s8, r8 = self._module_ote(df)
            total_long_score += l8
            total_short_score += s8
            if r8: reasons_log.append(f"[OTE: {r8}]")

            l9, s9, r9 = self._module_killzones()
            total_long_score += l9
            total_short_score += s9
            if r9: reasons_log.append(f"[Zaman: {r9}]")
            
            # --- KARAR ANI ---
            threshold = self.threshold
            final_signal = "HOLD"
            final_reason = " | ".join(reasons_log)
            
            # Sadece bir taraf eşiği geçerse sinyal ver
            # Eğer ikisi de yüksekse (kararsızlık) HOLD kalır veya puanı çok yüksek olanı seçeriz.
            if total_long_score >= threshold and total_long_score > total_short_score:
                final_signal = "LONG"
                #self.log(f"🧩 LONG Sinyali: Puan {total_long_score} Detay: {final_reason}")
                
                # -> DÖNÜŞ EKLENDİ
                return final_signal, total_long_score, final_reason

            elif total_short_score >= threshold and total_short_score > total_long_score:
                final_signal = "SHORT"
                #self.log(f"🧩 SHORT Sinyali: Puan {total_short_score} Detay: {final_reason}")
                
                # -> DÖNÜŞ EKLENDİ
                return final_signal, total_short_score, final_reason

            else:
                return "HOLD", 0, final_reason
        
        except Exception as e:
            self.log(f"❌ get_composite_score hatası: {e}")
            # HATA DURUMUNDA DA 3 DEĞER DÖNDÜR
            return "HOLD", 0, f"Hata: {str(e)}"
