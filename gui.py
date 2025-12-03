import tkinter as tk
from tkinter import ttk, scrolledtext
import threading
import time
import datetime
import requests 

class BotGUI:
    
    def __init__(self, root, start_callback=None, stop_callback=None, toggle_trading_callback=None, close_all_callback=None, emergency_stop_callback=None):
        self.root = root
        self.root.title("AllyGator v0.2")
        self.root.configure(bg="#1E2A38")
        
        # --- Callback Fonksiyonları ---
        self.start_callback = start_callback
        self.stop_callback = stop_callback
        self.toggle_trading_callback = toggle_trading_callback
        # Yeni eklenen callbackleri içeri alıyoruz
        self.close_all_callback = close_all_callback
        self.emergency_stop_callback = emergency_stop_callback
        # UI Değişkenleri
        self.is_running = False 
        self.trading_active = False
        self.mode_var = tk.IntVar(value=1)
        self.tg_var = tk.IntVar(value=0)
        
        # Kontrol Anahtarları
        self.rsi_check_var = tk.IntVar(value=1)
        self.boll_check_var = tk.IntVar(value=1)
        self.sentiment_var = tk.IntVar(value=0)
        self.isolated_var = tk.IntVar(value=0)
        
        # Giriş Widgetlarını Saklayacağımız Sözlük (Ayarların anlık okunması için)
        self.entry_widgets = {}

        # Varsayılan Ayarlar
        self.settings = {
            'min_volume': 400, 'budget_pct': 1, 'sl_pct': 4.0, 'tp_pct': 1.0, 
            'min_rsi': 35, 'max_rsi': 65, 'funding_max': 0.1, 'score_thresh': 14
        }

        # İstatistikler
        self.bot_realized_pnl = 0.0
        self.winning_trades = 0
        self.losing_trades = 0
        
        self.setup_gui()
        self.log("Bot başlatıldı.", force=True)
        self.toggle_mode_display()

    def setup_gui(self):
        self.root.state('zoomed')
        self.root.minsize(1000, 400)
        
        # Başlık
        header = tk.Frame(self.root, bg="#1E2A38")
        header.pack(fill=tk.X, padx=5, pady=2)
        tk.Label(header, text="AllyGator v0.2", font=("Arial", 16, "bold"), fg="#00D8FF", bg="#1E2A38").pack(pady=5)

        # Ana Panel
        main_pane = tk.PanedWindow(self.root, orient=tk.HORIZONTAL, bg="#1E2A38", sashrelief=tk.RAISED, sashwidth=4)
        main_pane.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # SOL PANEL
        left_outer = tk.Frame(main_pane, bg="#2C3E50")
        main_pane.add(left_outer, minsize=500)
        
        left_canvas = tk.Canvas(left_outer, bg="#2C3E50", highlightthickness=0)
        scrollbar_left = ttk.Scrollbar(left_outer, orient="vertical", command=left_canvas.yview)
        self.left_scrollable_frame = tk.Frame(left_canvas, bg="#2C3E50")
        
        self.left_scrollable_frame.bind("<Configure>", lambda e: left_canvas.configure(scrollregion=left_canvas.bbox("all")))
        
        left_canvas.create_window((0, 0), window=self.left_scrollable_frame, anchor="nw", width=480)
        left_canvas.configure(yscrollcommand=scrollbar_left.set)
        left_canvas.pack(side="left", fill="both", expand=True)
        scrollbar_left.pack(side="right", fill="y")

        # 1. HESAP ÖZETİ
        self.create_section_header(self.left_scrollable_frame, "KONTROL MERKEZİ")
        account_info = tk.Frame(self.left_scrollable_frame, bg="#2C3E50")
        account_info.pack(fill=tk.X, padx=5, pady=5)
        account_info.grid_columnconfigure((0,1), weight=1)

        self.lbl_balance = self.add_info_row(account_info, 0, 0, "Toplam Bakiye:", "-- $", "#2ECC71") 
        self.lbl_positions = self.add_info_row(account_info, 0, 1, "Açık Pozisyon:", "0", "#E67E22")
        self.lbl_total_pos_value = self.add_info_row(account_info, 1, 0, "Toplam İşlem Değeri:", "-- $", "#9B59B6") 
        self.lbl_pnl = self.add_info_row(account_info, 1, 1, "Günlük PNL:", "-- $", "#1ABC9C")
        self.lbl_bot_pnl = self.add_info_row(account_info, 2, 0, "Bot PNL:", "+0.00", "#2ECC71") 
        self.lbl_trade_count = self.add_info_row(account_info, 2, 1, "İşlem Sayısı :", "0", "#3498DB") 
        self.lbl_stats = self.add_info_row(account_info, 3, 0, "Kazanan / Kaybeden:", "0 / 0", "#E67E22") 
        self.lbl_runtime = self.add_info_row(account_info, 3, 1, "Çalışma Süresi:", "00:00", "#3498DB")

        # 2. KONTROL MERKEZİ
        self.create_section_header(self.left_scrollable_frame, " ") #, "KONTROL MERKEZİ")
        mode_frame = tk.Frame(self.left_scrollable_frame, bg="#2C3E50")
        mode_frame.pack(fill=tk.X, pady=5)
        
        self.mode_label = tk.Label(mode_frame, text="MOD: TESTNET (Güvenli)", font=("Arial", 12, "bold"), fg="#2ECC71", bg="#2C3E50")
        self.mode_label.pack(pady=5)
        
        chk_frame = tk.Frame(mode_frame, bg="#2C3E50")
        chk_frame.pack(pady=5)
        tk.Checkbutton(chk_frame, text="Testnet Kullan", variable=self.mode_var, command=self.toggle_mode_display, bg="#2C3E50", fg="white", selectcolor="#2C3E50", activebackground="#2C3E50").pack(side=tk.LEFT, padx=10)
        tk.Checkbutton(chk_frame, text="Telegram Aktif", variable=self.tg_var, bg="#2C3E50", fg="#00D8FF", selectcolor="#2C3E50", activebackground="#2C3E50").pack(side=tk.LEFT, padx=10)

        # Butonlar
        btn_frame = tk.Frame(self.left_scrollable_frame, bg="#2C3E50")
        btn_frame.pack(fill=tk.X, pady=10, padx=5)
        
        self.btn_start = tk.Button(btn_frame, text="▶️ BAŞLAT", bg="#2980B9", fg="#F9FBFC", font=("Arial", 9), height=1, command=self.on_start_press)
        self.btn_start.pack(side=tk.LEFT, padx=2, expand=True, fill=tk.X)
        self.btn_stop = tk.Button(btn_frame, text="⏹️ DURDUR", bg="#E74C3C", fg="white", font=("Arial", 9), height=1, state=tk.DISABLED, command=self.on_stop_press)
        self.btn_stop.pack(side=tk.LEFT, padx=2, expand=True, fill=tk.X)
        self.btn_alim_ac = tk.Button(btn_frame, text="🟢 ALIM AÇ", bg="#27AE60", fg="white", font=("Arial", 9), height=1, state=tk.DISABLED, command=self.toggle_trading_ui)
        self.btn_alim_ac.pack(side=tk.LEFT, padx=2, expand=True, fill=tk.X)
        self.btn_alim_kapa = tk.Button(btn_frame, text="🔴 ALIM KAPA", bg="#E74C3C", fg="white", font=("Arial", 9), height=1, state=tk.DISABLED, command=self.toggle_trading_ui)
        self.btn_alim_kapa.pack(side=tk.LEFT, padx=2, expand=True, fill=tk.X)
        
        # Acil Durum Butonları
        # Acil Durum Butonları
        emergency_frame = tk.Frame(self.left_scrollable_frame, bg="#2C3E50")
        emergency_frame.pack(fill=tk.X, pady=5, padx=5)
        emergency_frame.grid_columnconfigure((0, 1), weight=1)
        
        # self.close_all_callback varsa onu kullan, yoksa boş geç
        cmd_close = self.close_all_callback if self.close_all_callback else None
        self.btn_close_all = tk.Button(emergency_frame, text="❌ TÜM POZİSYONLARI KAPAT", bg="#F39C12", fg="#F4F5F0", width=20, command=cmd_close, font=("Arial", 9))
        self.btn_close_all.grid(row=0, column=0, padx=2, pady=2, sticky="ew")
        
        # self.emergency_stop_callback varsa onu kullan
        cmd_emergency = self.emergency_stop_callback if self.emergency_stop_callback else None
        self.btn_emergency = tk.Button(emergency_frame, text="🛑 ACİL DURDUR", bg="#C0392B", fg="white", width=20, command=cmd_emergency, font=("Arial", 9))
        self.btn_emergency.grid(row=0, column=1, padx=2, pady=2, sticky="ew")

        # 3. STRATEJİ KRİTERLERİ
        self.create_section_header(self.left_scrollable_frame, " ") #, "STRATEJİ KRİTERLERİ")
        settings_grid = tk.Frame(self.left_scrollable_frame, bg="#2C3E50")
        settings_grid.pack(padx=5, pady=5)

        numerical_labels = ["Min Hacim (M)", "Bütçe (%)", "SL %", "ATR-TP %", "Min RSI", "Max RSI", "Funding Max", "Puan Eşiği"]
        numerical_keys = ['min_volume', 'budget_pct', 'sl_pct', 'tp_pct', 'min_rsi', 'max_rsi', 'funding_max', 'score_thresh']
        
        for i, (txt, key) in enumerate(zip(numerical_labels, numerical_keys)):
            r, c = divmod(i, 4)
            tk.Label(settings_grid, text=txt, bg="#2C3E50", fg="#BDC3C7", font=("Arial", 10)).grid(row=r*2, column=c, padx=5, pady=2)
            entry = tk.Entry(settings_grid, width=8, bg="#34495E", fg="#F4F5F0", insertbackground="white")
            entry.insert(0, str(self.settings.get(key, "")))
            entry.grid(row=r*2+1, column=c, padx=5, pady=(2,5) ) #pady=(0, 10))
            
            # --- DEĞİŞİKLİK: Widget'ı sakla ki main.py doğrudan okuyabilsin ---
            self.entry_widgets[key] = entry 
            entry.bind('<FocusOut>', lambda e, k=key, ent=entry: self.update_setting(k, ent))

        # Checkboxlar
        check_boxes = [("RSI Kontrolü", self.rsi_check_var), ("BOLL Kriteri", self.boll_check_var), ("Yerel AI Aç/Kapa", self.sentiment_var), ("Isolated Mod", self.isolated_var)]
        check_frame = tk.Frame(settings_grid, bg="#2C3E50")
        check_frame.grid(row=4, column=0, columnspan=4, pady=10)
        for i, (txt, var) in enumerate(check_boxes):
            tk.Checkbutton(check_frame, text=txt, variable=var, bg="#2C3E50", fg="white", selectcolor="#2C3E50", activebackground="#2C3E50").pack(side=tk.LEFT, padx=5)

        # SAĞ PANEL (Aynı kaldı)
        right_pane = tk.PanedWindow(main_pane, orient=tk.VERTICAL, bg="#1E2A38", sashrelief=tk.RAISED, sashwidth=4)
        main_pane.add(right_pane, minsize=500)

        positions_frame = tk.Frame(right_pane, bg="#2C3E50")
        right_pane.add(positions_frame, minsize=300)
        tk.Label(positions_frame, text="AÇIK POZİSYONLAR", font=("Arial", 12, "bold"), fg="#00D8FF", bg="#2C3E50").pack(pady=5)
        
        pos_container = tk.Frame(positions_frame, bg="#2C3E50")
        pos_container.pack(fill=tk.BOTH, padx=10, pady=5)  #, expand=True
        canvas = tk.Canvas(pos_container, bg="#2C3E50", highlightthickness=0)
        scrollbar = ttk.Scrollbar(pos_container, orient="vertical", command=canvas.yview)
        self.scrollable_frame = tk.Frame(canvas, bg="#2C3E50")
        self.scrollable_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        
        log_frame = tk.Frame(right_pane, bg="#2C3E50")
        right_pane.add(log_frame, minsize=200)
        tk.Label(log_frame, text="SİSTEM LOGLARI", font=("Arial", 11, "bold"), fg="#00D8FF", bg="#2C3E50").pack(pady=2)
        self.log_text = scrolledtext.ScrolledText(log_frame, bg="#1C2833", fg="#2ECC71", font=("Consolas", 10), height=10)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)
        
        status_frame = tk.Frame(self.root, bg="#2C3E50")
        status_frame.pack(fill=tk.X, padx=5, pady=2)
        self.status_label = tk.Label(status_frame, text="🟢 Sistem Hazır - Beklemede", font=("Arial", 9), fg="white", bg="#2C3E50")
        self.status_label.pack(side=tk.LEFT, expand=True, anchor="w")
        self.last_update_label = tk.Label(status_frame, text="Son Güncelleme: --", font=("Arial", 9), fg="#BDC3C7", bg="#2C3E50")
        self.last_update_label.pack(side=tk.RIGHT, padx=10)

    # --- YARDIMCI METOD: GÜNCEL AYARLARI ZORLA OKU ---
    def get_latest_settings_from_ui(self):
        """Kutucuklarda ne yazıyorsa anında onu okur, FocusOut beklemez."""
        for key, widget in self.entry_widgets.items():
            try:
                val = float(widget.get())
                self.settings[key] = val
            except ValueError:
                pass # Boşsa veya hatalıysa eski değeri koru
        return self.settings

    def create_section_header(self, parent, text):
        tk.Label(parent, text=text, font=("Arial", 12, "bold"), fg="#00D8FF", bg="#2C3E50").pack(pady=(5, 5))

    def add_info_row(self, parent, r, c, title, val, color):
        frame = tk.Frame(parent, bg="#2C3E50")
        frame.grid(row=r, column=c, sticky="ew", padx=5, pady=2)
        tk.Label(frame, text=title, font=("Arial", 11), fg="#BDC3C7", bg="#2C3E50", anchor="w").pack(side=tk.LEFT)
        val_lbl = tk.Label(frame, text=val, font=("Arial", 11, "bold"), fg=color, bg="#2C3E50", anchor="e")
        val_lbl.pack(side=tk.RIGHT)
        return val_lbl

    def update_setting(self, key, entry_widget):
        try:
            val = float(entry_widget.get())
            self.settings[key] = val
        except ValueError:
            self.log(f"HATA: {key} geçersiz!", force=True)

    def toggle_mode_display(self):
        if self.mode_var.get() == 1:
            self.mode_label.config(text="MOD: TESTNET (Güvenli)", fg="#2ECC71")
        else:
            self.mode_label.config(text="MOD: GERÇEK (Mainnet)", fg="#E74C3C")

    # Aksiyonlar
    def on_start_press(self):
        self.is_running = True
        self.btn_start.config(state=tk.DISABLED, bg="#145A32")
        self.btn_stop.config(state=tk.NORMAL, bg="#E74C3C")
        self.btn_alim_ac.config(state=tk.NORMAL, bg="#27AE60")
        self.btn_alim_kapa.config(state=tk.DISABLED, bg="#E74C3C") 
        leverage_mode = "Isolated" if self.isolated_var.get() == 1 else "Cross (Varsayılan)"
        self.log(f"Bot başlatılıyor. Mod: {'TESTNET' if self.mode_var.get() == 1 else 'MAİN NET'}. Kaldıraç: {leverage_mode}", force=True)
        self.status_label.config(text="🟡 Bot Çalışıyor - Alım Modu: Kapalı")
        if self.start_callback: threading.Thread(target=self.start_callback, daemon=True).start()

    def on_stop_press(self):
        self.is_running = False
        self.trading_active = False
        self.btn_start.config(state=tk.NORMAL, bg="#2980B9")
        self.btn_stop.config(state=tk.DISABLED, bg="#922B21")
        self.btn_alim_ac.config(text="🟢 ALIM AÇ", bg="#27AE60", state=tk.DISABLED)
        self.btn_alim_kapa.config(state=tk.DISABLED, bg="#E74C3C")
        self.log("🛑 Bot durduruldu.", force=True)
        self.status_label.config(text="🔴 Bot Durduruldu - Sistem Pasif")
        if self.stop_callback: self.stop_callback()

    def toggle_trading_ui(self):
        if self.btn_alim_ac['state'] == tk.NORMAL:
            self.trading_active = True
            self.btn_alim_ac.config(state=tk.DISABLED, bg="#145A32")
            self.btn_alim_kapa.config(state=tk.NORMAL, bg="#E74C3C")
            self.log("✅ ALIM MODU AÇIK: Sinyal Tarama Başladı.", force=True)
            self.status_label.config(text="🟢 Bot Çalışıyor - Alım Modu: Açık")
        elif self.btn_alim_kapa['state'] == tk.NORMAL:
            self.trading_active = False
            self.btn_alim_ac.config(state=tk.NORMAL, bg="#27AE60")
            self.btn_alim_kapa.config(state=tk.DISABLED, bg="#922B21")
            self.log("🛑 ALIM MODU KAPALI: Tarama Durduruldu.", force=True)
            self.status_label.config(text="🟡 Bot Çalışıyor - Alım Modu: Kapalı")
        if self.toggle_trading_callback: self.toggle_trading_callback(self.trading_active)

    def close_all_positions_ui(self):
        self.log("⚠️ BUTONA BASILDI (Fonksiyon Bağlanmadı!)", force=True)

    def emergency_stop_ui(self):
        self.log("🛑 ACİL DURDURMA!", force=True)
        self.trading_active = False
        self.on_stop_press()
        self.close_all_positions_ui() # Bunu da çağır

    def log(self, message, force=False):
        timestamp = datetime.datetime.now().strftime("%H:%M:%S")
        full_msg = f"[{timestamp}] {message}\n"
        def _write():
            self.log_text.insert(tk.END, full_msg)
            self.log_text.see(tk.END)
        if threading.current_thread() is threading.main_thread(): _write()
        else: self.root.after(0, _write)

if __name__ == "__main__":
    root = tk.Tk()
    app = BotGUI(root)
    root.mainloop()
