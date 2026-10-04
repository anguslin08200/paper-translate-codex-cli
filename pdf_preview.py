"""Page-at-a-time high-resolution PDF preview for the desktop translator."""
from __future__ import annotations
import tkinter as tk
from tkinter import ttk, messagebox


class PDFPreview(tk.Toplevel):
    def __init__(self, parent, path):
        # Read the vector PDF directly and rerender on every zoom; never enlarge a cached thumbnail.
        import pymupdf
        super().__init__(parent)
        self.title(f"高畫質預覽 · {path.name}")
        self.geometry("1000x850")
        self.document = pymupdf.open(path)
        self.page_number = 0
        self.zoom = tk.StringVar(value="150%")
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        ttk.Button(bar, text="上一頁", command=lambda: self.move(-1)).pack(side="left")
        ttk.Button(bar, text="下一頁", command=lambda: self.move(1)).pack(side="left")
        self.counter = ttk.Label(bar)
        self.counter.pack(side="left", padx=12)
        combo = ttk.Combobox(bar, textvariable=self.zoom, values=("100%", "150%", "200%", "300%"),
                             width=8, state="readonly")
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda event: self.render())
        ttk.Label(bar, text="縮放時重新渲染原始 PDF；雙語頁面可橫向滾動。 ").pack(side="left", padx=8)
        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(frame, background="#555555")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=self.canvas.xview)
        vertical = ttk.Scrollbar(frame, orient="vertical", command=self.canvas.yview)
        horizontal.grid(row=1, column=0, sticky="ew")
        vertical.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(xscrollcommand=horizontal.set, yscrollcommand=vertical.set)
        self.canvas.bind("<MouseWheel>", lambda event: self.canvas.yview_scroll(-int(event.delta / 120), "units"))
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.render()

    def move(self, offset):
        # Render a single page to keep memory usage bounded for long papers and books.
        self.page_number = max(0, min(len(self.document) - 1, self.page_number + offset))
        self.render()

    def render(self):
        import pymupdf
        try:
            page = self.document[self.page_number]
            factor = self.winfo_fpixels("1i") / 72 * int(self.zoom.get().rstrip("%")) / 100
            # Bound unusually large engineering pages while retaining crisp ordinary paper pages.
            factor = min(factor, 6000 / max(page.rect.width, page.rect.height))
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(factor, factor), alpha=False)
            self.photo = tk.PhotoImage(data=pixmap.tobytes("png"))
            self.canvas.delete("all")
            self.canvas.create_image(0, 0, image=self.photo, anchor="nw")
            self.canvas.configure(scrollregion=(0, 0, pixmap.width, pixmap.height))
            self.counter.configure(text=f"第 {self.page_number + 1} / {len(self.document)} 頁")
        except Exception as exc:
            messagebox.showerror("預覽失敗", str(exc), parent=self)

    def close(self):
        self.document.close()
        self.destroy()
