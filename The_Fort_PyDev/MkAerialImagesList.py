import csv
import json
import re
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox

IMAGE_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff",
    ".webp", ".heic", ".heif"
}

# Matches: RockSpringRd_120925_Aerial (192)
AERIAL_NAME_RE = re.compile(r"^RockSpringRd_\d{6}_Aerial \(\d+\)$")

SETTINGS_PATH = Path.home() / ".image_list_to_csv_settings.json"


class ImageListToCsvApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Image List -> CSV")
        self.geometry("720x230")

        self.selected_dir = ""
        self.output_csv = ""
        self.last_image_dir = ""
        self.last_csv_dir = ""

        self.load_settings()

        # --- UI ---
        self.dir_label = tk.Label(self, text="Input directory: (none)", anchor="w", justify="left")
        self.dir_label.pack(fill="x", padx=12, pady=(12, 4))

        self.btn_pick_dir = tk.Button(self, text="Select Image Directory…", command=self.pick_directory)
        self.btn_pick_dir.pack(anchor="w", padx=12, pady=(0, 10))

        self.csv_label = tk.Label(self, text="Output CSV: (none)", anchor="w", justify="left")
        self.csv_label.pack(fill="x", padx=12, pady=(0, 4))

        self.btn_pick_csv = tk.Button(self, text="Select Output CSV…", command=self.pick_output_csv, state="disabled")
        self.btn_pick_csv.pack(anchor="w", padx=12, pady=(0, 14))

        tk.Button(self, text="Quit", command=self.quit_app).pack(anchor="w", padx=12)

        self.protocol("WM_DELETE_WINDOW", self.quit_app)

        # Reflect loaded input dir if present
        if self.selected_dir:
            self.dir_label.config(text=f"Input directory: {self.selected_dir}")
            self.btn_pick_csv.config(state="normal")

    def load_settings(self):
        if not SETTINGS_PATH.exists():
            return
        try:
            data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            self.last_image_dir = (data.get("last_image_dir") or "").strip()
            self.last_csv_dir = (data.get("last_csv_dir") or "").strip()

            if self.last_image_dir and Path(self.last_image_dir).is_dir():
                self.selected_dir = self.last_image_dir
        except Exception:
            pass

    def save_settings(self):
        try:
            data = {
                "last_image_dir": self.last_image_dir,
                "last_csv_dir": self.last_csv_dir,
            }
            SETTINGS_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass

    def quit_app(self):
        self.save_settings()
        self.destroy()

    def pick_directory(self):
        initial = self.last_image_dir if self.last_image_dir and Path(self.last_image_dir).is_dir() else str(Path.home())
        directory = filedialog.askdirectory(
            title="Select directory containing images",
            initialdir=initial
        )
        if directory and Path(directory).is_dir():
            self.selected_dir = directory
            self.last_image_dir = directory
            self.dir_label.config(text=f"Input directory: {directory}")

            # Enable output selection once input is valid
            self.btn_pick_csv.config(state="normal")
            self.save_settings()

    def pick_output_csv(self):
        if not self.selected_dir or not Path(self.selected_dir).is_dir():
            messagebox.showwarning("Missing input", "Please select a valid input directory first.")
            self.btn_pick_csv.config(state="disabled")
            return

        initial_dir = self.last_csv_dir if self.last_csv_dir and Path(self.last_csv_dir).is_dir() else self.selected_dir

        filename = filedialog.asksaveasfilename(
            title="Select output CSV file",
            initialdir=initial_dir,
            initialfile="image_list.csv",
            defaultextension=".csv",
            filetypes=[("CSV Files", "*.csv"), ("All Files", "*.*")]
        )
        if not filename:
            return

        if not filename.lower().endswith(".csv"):
            filename += ".csv"

        self.output_csv = filename
        self.last_csv_dir = str(Path(filename).parent)
        self.csv_label.config(text=f"Output CSV: {filename}")
        self.save_settings()

        # Immediately write CSV after selecting output file
        self.write_csv()

    def list_images(self, directory: str):
        p = Path(directory)
        if not p.exists() or not p.is_dir():
            raise FileNotFoundError(f"Not a valid directory: {directory}")

        files = []
        for child in p.iterdir():
            if not child.is_file():
                continue

            # Extension filter
            if child.suffix.lower() not in IMAGE_EXTS:
                continue

            # Filename pattern filter (stem excludes extension)
            if not AERIAL_NAME_RE.match(child.stem):
                continue

            files.append(str(child.resolve()))

        files.sort(key=lambda s: s.lower())
        return files

    def write_csv(self):
        if not self.selected_dir or not Path(self.selected_dir).is_dir():
            messagebox.showwarning("Missing input", "Please select a valid input directory.")
            return
        if not self.output_csv:
            messagebox.showwarning("Missing output", "Please select an output CSV file.")
            return

        try:
            images = self.list_images(self.selected_dir)

            with open(self.output_csv, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["image_path"])
                for path in images:
                    w.writerow([path])

            # Persist last used dirs
            self.last_image_dir = self.selected_dir
            self.last_csv_dir = str(Path(self.output_csv).parent)
            self.save_settings()

            messagebox.showinfo(
                "Done",
                f"Wrote {len(images)} matching image file(s) to:\n{self.output_csv}"
            )
        except Exception as e:
            messagebox.showerror("Error", str(e))


def main():
    app = ImageListToCsvApp()
    app.mainloop()


if __name__ == "__main__":
    main()
