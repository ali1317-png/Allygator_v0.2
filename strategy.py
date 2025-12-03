import pandas as pd
import time
# Yeni beyin takımını import ediyoruz
from strategies.score import SignalEngine

class StrategyCore:
    def __init__(self, api_client, settings, log_func):
        self.client = api_client
        self.settings = settings
        self.log = log_func
        self.symbols_to_scan = [] 
        
        # Sinyal motorunu başlatıyoruz
        self.engine = SignalEngine(
            settings=self.settings,  # GUI'den gelen dict'i geçir
            log_func=log_func
        )

    def get_symbols_to_scan(self):
        """Hacim filtresine göre taranacak coinleri bulur."""
        self.log("🔍 Coin taraması başlatılıyor...")
        try:
            tickers = self.client.ticker_24hr_price_change()
            
            try:
                min_vol_mn = float(self.settings.get('min_volume', 130))
            except:
                min_vol_mn = 130.0
            
            target_volume = min_vol_mn * 1_000_000
            final_symbols = []
            
            for t in tickers:
                symbol = t['symbol']
                if symbol.endswith('USDT') and symbol != 'USDTUSDT':
                    try:
                        vol = float(t['quoteVolume'])
                        
                        # SADECE BİR KOŞUL: target_volume'dan büyük mü?
                        if vol >= target_volume:
                            
                            final_symbols.append(symbol)
                                
                    except Exception as e:
                        continue
            
            self.symbols_to_scan = final_symbols
            self.log(f"✅ Filtreyi geçen: {len(self.symbols_to_scan)} sembol.")
            return final_symbols
                    
        except Exception as e:
            self.log(f"❌ Tarama Hatası: {e}", True)
            self.symbols_to_scan = []
        return self.symbols_to_scan

    def get_candlesticks(self, symbol, interval, limit=100):
        """Mum verilerini çeker."""
        for i in range(3):
            try:
                klines = self.client.klines(symbol, interval, limit=limit)
                df = pd.DataFrame(klines, columns=[
                    'Open time', 'Open', 'High', 'Low', 'Close', 'Volume', 
                    'Close time', 'Quote asset volume', 'Number of trades', 
                    'Taker buy base asset volume', 'Taker buy quote asset volume', 'Ignore'
                ])
                if df is None or len(df) < 20:
                    return None
                cols = ['Open', 'High', 'Low', 'Close', 'Volume']
                df[cols] = df[cols].astype(float)
                
                # Eksik/bozuk veri kontrolü
                if df['Close'].isna().any() or (df['High'] < df['Low']).any():
                    self.log(f"❌ Bozuk veri: {symbol}")
                    return None
                return df
            
            except Exception as e:
                time.sleep(1)
        return None

    def calculate_volatility(self, df):
        # SENİN ORİJİNAL KODUN (DOKUNULMADI)
        try:
            if df is None or len(df) < 40:
                return 1, 0.0
            
            # Sütun isimleri büyük harf uyumu
            close_col = 'Close' if 'Close' in df.columns else 'close'
            
            df['Change_Pct'] = df[close_col].pct_change().abs() * 100
            avg_change_pct = df['Change_Pct'].iloc[-40:].mean() 
            
            if avg_change_pct >= 1.0: leverage = 1
            elif avg_change_pct > 0.4: leverage = 2
            elif avg_change_pct > 0.2: leverage = 3
            else: leverage = 5
            
            return leverage, avg_change_pct
        except Exception as e:
        # Hata durumunda da 2 değer döndür
            self.log(f"❌ Bozuk volatility: ")
            return 1, 0.0

    def generate_signal(self, df, symbol=None):
        """
        client parametresini kaldırıyoruz - gereksiz karmaşıklık
        """
        # SADECE df parametresini gönder
        signal, score, reason = self.engine.get_composite_score(df)
        
        # Mevcut RSI ve BB hesaplaması
        if 'rsi' not in df.columns:
            df = self.engine.calculate_indicators(df)
            
        rsi = df['rsi'].iloc[-1]
        bb_upper = df['bb_upper'].iloc[-1]
        bb_lower = df['bb_lower'].iloc[-1]
        
        return signal, reason, rsi, bb_upper, bb_lower
