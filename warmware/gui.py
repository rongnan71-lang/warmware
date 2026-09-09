"""
tkinter GUI — 暖炉操作面板
=========================
简单易懂：一个大开关、实时温度/状态、模式选择、安全信息。
用队列把后台线程的更新安全地投递到主线程。
"""

from __future__ import annotations

import queue
import tkinter as tk
from tkinter import messagebox, ttk


def _safe_after(root, ms, fn):
    try:
        root.after(ms, fn)
    except Exception:
        pass


class WarmWareGUI:
    def __init__(self, engine, guardian, caps, baseline):
        self.engine = engine
        self.guardian = guardian
        self.caps = caps
        self.baseline = baseline
        self.q: queue.Queue = queue.Queue()

        self._build()

    # ------------------------------------------------------------------
    def _build(self):
        self.root = tk.Tk()
        self.root.title("🔥 WarmWare 暖炉电脑 v2.3")
        self.root.geometry("440x640")
        self.root.resizable(False, False)
        try:
            self.root.configure(bg="#1e1e2e")
        except Exception:
            pass

        pad = {"padx": 16, "pady": 8}

        # 标题
        title = tk.Label(self.root, text="🔥 WarmWare 暖炉电脑",
                         font=("Microsoft YaHei UI", 20, "bold"), fg="#ffb454", bg="#1e1e2e")
        title.pack(pady=(20, 4))
        sub = tk.Label(self.root, text="冬天没有小太阳？拿电脑取暖！",
                       font=("Microsoft YaHei UI", 11), fg="#cdd6f4", bg="#1e1e2e")
        sub.pack()

        # 硬件信息
        self.hw_label = tk.Label(self.root, text=self.caps.summary(),
                                 font=("Consolas", 10), fg="#a6adc8", bg="#1e1e2e",
                                 justify="left")
        self.hw_label.pack(**pad)

        # 大开关
        self.switch_btn = tk.Button(self.root, text="🔥 点火取暖",
                                    font=("Microsoft YaHei UI", 18, "bold"),
                                    bg="#313244", fg="#ffb454", activebackground="#45475a",
                                    activeforeground="#ffb454",
                                    relief="flat", bd=0, height=2, cursor="hand2",
                                    command=self._on_switch)
        self.switch_btn.pack(**pad, fill="x")

        # 模式选择
        mode_frame = tk.Frame(self.root, bg="#1e1e2e")
        mode_frame.pack(**pad, fill="x")
        tk.Label(mode_frame, text="模式：", font=("Microsoft YaHei UI", 11),
                 fg="#cdd6f4", bg="#1e1e2e").pack(side="left")
        self.mode_var = tk.StringVar(value="auto")
        self.mode_combo = ttk.Combobox(mode_frame, textvariable=self.mode_var,
                                       state="readonly", width=18, font=("Microsoft YaHei UI", 10))
        self.mode_combo["values"] = [
            "auto", "仅 CPU", "仅 CPU（低功率）",
            "GPU 为主", "双引擎", "双引擎（低功率）",
            "激进模式（烧得猛）", "关闭"
        ]
        self.mode_combo.pack(side="left")
        self.mode_combo.bind("<<ComboboxSelected>>", self._on_mode_change)

        # 状态区
        self.status_text = tk.Text(self.root, height=5, font=("Consolas", 10),
                                   bg="#11111b", fg="#a6adc8", relief="flat",
                                   state="disabled", wrap="word")
        self.status_text.pack(**pad, fill="both", expand=True)

        # 安全报告
        self.safety_label = tk.Label(self.root, text="", font=("Consolas", 10),
                                     fg="#a6e3a1", bg="#1e1e2e", justify="left")
        self.safety_label.pack(**pad, fill="x")

        # 底部
        self.mode_label = tk.Label(self.root, text="当前模式：关闭",
                                   font=("Microsoft YaHei UI", 10), fg="#89b4fa", bg="#1e1e2e")
        self.mode_label.pack(side="bottom", pady=6)
        hint = tk.Label(self.root, text="安全守护会实时压住负载，绝不把系统搞死",
                        font=("Microsoft YaHei UI", 9), fg="#6c7086", bg="#1e1e2e")
        hint.pack(side="bottom")

        self._append_status(self.caps.summary())
        if self.caps.gpu_available and not self.engine.gpu.available:
            self._append_status(f"\n[提示] {self.engine.gpu._error}，仅 CPU 产热。")

        # 定时轮询后台队列
        self._poll()

    # ------------------------------------------------------------------
    def _append_status(self, msg: str):
        self.status_text.configure(state="normal")
        self.status_text.insert("end", msg + "\n")
        self.status_text.see("end")
        self.status_text.configure(state="disabled")

    def _on_switch(self):
        if self.engine.running:
            self.engine.stop()
            self.guardian.stop()
            self.switch_btn.configure(text="🔥 点火取暖", bg="#313244", fg="#ffb454")
            self._append_status("\n—— 暖炉已熄灭 ——")
            self._update_mode_label()
        else:
            self._start_heating()

    def _start_heating(self):
        # 采样基准线
        self._append_status("🔍 正在感知系统环境（5 秒）…")
        self.root.update_idletasks()
        ok, baseline = self.guardian.take_baseline(on_log=self._append_status)
        if not ok:
            messagebox.showerror("WarmWare", "无法采集系统基准线，无法安全启动。")
            return
        # 推荐模式
        rec = self.engine.detect_recommend()
        self.engine.last_recommend = rec
        if self.mode_var.get() == "auto":
            mode = rec
        else:
            mode = self._label_to_mode(self.mode_var.get())
        # 模式为"关闭"时不产热，直接返回（避免进入"运行但零产热"的怪异态）
        if mode == "off":
            self._append_status("已选择「关闭」，未点火。请选其它模式。")
            return
        success, msg = self.engine.start(mode)
        if not success:
            messagebox.showerror("WarmWare", msg)
            return
        # 关联引擎，让安全循环能扣除暖炉自己制造的负载
        self.guardian.set_engine(self.engine)
        # 挂上安全回调
        self.guardian.start_safety_loop(
            on_update=lambda d: self.q.put(("safety", d)),
            on_alert=lambda lv, m: self.q.put(("alert", m)),
        )
        self.switch_btn.configure(text="🧯 熄灭取暖", bg="#45475a", fg="#f38ba8")
        self._append_status(f"\n{msg}")
        self._update_mode_label()
        self._render_safety(self.guardian.reserve_report())

    def _label_to_mode(self, label: str) -> str:
        m = {"仅 CPU": "cpu_only", "仅 CPU（低功率）": "cpu_only_low",
             "GPU 为主": "gpu_primary", "双引擎": "hybrid",
             "双引擎（低功率）": "hybrid_low",
             "激进模式（烧得猛）": "aggressive", "关闭": "off"}
        return m.get(label, "hybrid")

    def _on_mode_change(self, _evt=None):
        # 切换下拉框时，如果正在运行就立即应用（下次安全回调会微调）
        if self.engine.running:
            label = self.mode_var.get()
            if label != "auto":
                self.engine.apply_mode(self._label_to_mode(label))
                self._append_status(f"\n已切换到：{label}")
            self._update_mode_label()

    def _update_mode_label(self):
        if self.engine.running:
            self.mode_label.configure(text=f"当前模式：{self.engine.status()['mode_label']}")
        else:
            self.mode_label.configure(text="当前模式：关闭")

    # ------------------------------------------------------------------
    def _render_safety(self, rep: dict):
        self.safety_label.configure(
            text=(
                f"🛡️ CPU 保护区 {rep['cpu_reserve']*100:.0f}%  可用 {rep['cpu_available']*100:.0f}%\n"
                f"🛡️ GPU 保护区 {rep['gpu_reserve']*100:.0f}%  可用 {rep['gpu_available']*100:.0f}\n"
                f"🛡️ 内存保护区 {rep['mem_reserve']*100:.0f}%  可用 {rep['mem_available']*100:.0f}\n"
                f"🛡️ 最大负载 {rep['max_load']:.1f}  (每核余量 {0.30:.2f})"
            )
        )

    def _poll(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "safety":
                    d = payload
                    self.engine.adjust_from_safety(
                        d["bottleneck"], action=d.get("action", "hold"))
                    self._render_safety(self.guardian.reserve_report())
                    self.status_text.configure(state="normal")
                    self.status_text.delete("1.0", "end")
                    gb = self.engine.gpu.backend.upper() if self.engine.gpu.available else "无GPU"
                    ac = {"hold": "保持", "ramp_up": "升载", "ramp_down": "降载", "emergency": "紧急降载"}.get(d.get("action"), "保持")
                    self.status_text.insert("end",
                        f"CPU 负载 {d['current_cpu']*100:.0f}% | "
                        f"GPU {d['current_gpu']*100:.0f}% ({gb}) | "
                        f"内存 {d['current_mem']*100:.0f}%\n"
                        f"外部负载  CPU {d.get('external_cpu',0)*100:.0f}%  GPU {d.get('external_gpu',0)*100:.0f}%  系统 {d['current_load']:.2f}\n"
                        f"瓶颈余量 {d['bottleneck']*100:.0f}%  动作：{ac}\n"
                        f"引擎因子  CPU {self.engine.cpu.factor*100:.0f}%  "
                        f"GPU {self.engine.gpu.factor*100:.0f}%\n"
                    )
                    self.status_text.configure(state="disabled")
                    self._update_mode_label()
                elif kind == "alert":
                    self._append_status(payload)
        except queue.Empty:
            pass
        _safe_after(self.root, 250, self._poll)

    def run(self):
        self.root.mainloop()

    def close(self):
        try:
            self.engine.stop()
            self.guardian.stop()
            self.root.destroy()
        except Exception:
            pass