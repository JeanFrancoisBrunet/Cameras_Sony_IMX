#!/usr/bin/env python3
# ===============================================================================
#  Pilotage des caméras Sony IMX219 - 120° / IMX708 Wide 120° Autofocus / IMX500
#  version intégrant 3 caméras sur Raspberry Pi5 :
#           Port Caméra 0 : IMX219 ou IMX708 (connexion nappe en alternance)
#               Exposition manuelle (--shutter µs + --gain)
#               Balance des blancs manuelle (--awbgains r,b)
#               Miniature de la dernière photo capturée
#               Mode rafale configurable (N captures & délai)
#           Port Caméra 1 : IMX500
#               seuil de confiance configurable pour la détection d'objets
#               export log des détections IA en CSV ou JSON (hors rpicam-app)
#           Motion : compatible IMX219 et IMX708
#           Timelapse : IMX708 et/ou IMX500
#               AWB lock + AE lock (--awb off --awbgains) anti-scintillement
#               reprise de session via resume_state.json
#               watchdog (arrêt auto si N erreurs consécutives)
#               estimations taille frames + vidéo MP4 + espace disque dispo
#           Slow Motion : capture HFR + conversion ralenti
#           Fichiers : explorateur minimaliste du dossier de sauvegarde
#           Barre de statut système : température CPU + espace disque
#
#  Auteur  : Jean‑François BRUNET - JFBConseils - Avril / Juillet 2026
# ===============================================================================

import subprocess
import datetime
import os
import threading
import time
import webbrowser
import json
import shutil
import csv
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import Optional
from PIL import Image, ImageTk

import sys
sys.path.insert(0, "/home/jfbrunet/Projects/Telegram")
from motion_mode_manager import set_mode, get_mode

# ------------------------------------------------------------
# Chemins
# ------------------------------------------------------------
SCRIPT_DIR       = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SAVE_DIR = os.path.expanduser("~")          # ~/  sur n'importe quel Pi
IMX500_DEMOS     = SCRIPT_DIR
ICON_DIR         = os.path.join(SCRIPT_DIR, "icons")  # icons d'illustrations

# Dossier Motion captures
MOTION_CAPTURES_DIR = os.path.join(DEFAULT_SAVE_DIR, "motion_captures")

# ------------------------------------------------------------
# Motion – configuration
# ------------------------------------------------------------
MOTION_WEB_CTRL   = "http://localhost:8080"
MOTION_WEB_STREAM = "http://localhost:8081"
MOTION_DEV_IMX219 = "/dev/video0"
MOTION_DEV_IMX708 = "/dev/video8"
SYSTEMCTL         = "/usr/bin/systemctl"
LIBCAMERIFY       = "/usr/bin/libcamerify"

MOTION_SYSTEMCTL_TIMEOUT = 25
MOTION_STOP_GRACE        = 3

# ------------------------------------------------------------
# IMX708 Wide – caractéristiques
# ------------------------------------------------------------
IMX708_LENS_INFINITY = 0.0
IMX708_LENS_MIN      = 0.0
IMX708_LENS_MAX      = 10.0

# ------------------------------------------------------------
# Slow Motion – configuration
# ------------------------------------------------------------
SM_CAPTURE_MODES = [
    ("640 × 480  –  120 fps  (IMX219 / IMX708)", 640,  480,  120),
    ("640 × 480  –   60 fps  (IMX219 / IMX708)", 640,  480,   60),
    ("1280 × 720  –  60 fps  (IMX708 uniquement)", 1280, 720,   60),
]
SM_OUTPUT_FPS = 30

# ------------------------------------------------------------
# Timelapse – watchdog & estimations
# ------------------------------------------------------------
TL_MAX_ERREURS_CONSECUTIVES = 3

# Taille JPEG estimée (Ko min, Ko max) par résolution
TL_JPEG_SIZE_TABLE = {
    (4608, 2592): (1800, 3000),
    (2304, 1296): (700,  1300),
    (4056, 3040): (1500, 2800),
    (1920, 1080): (300,  700),
    (1280, 720):  (150,  400),
}
TL_JPEG_DEFAULT_SIZE = (400, 1000)

# Débit libx264 CRF23 estimé (kbps min, kbps max) par résolution
TL_VIDEO_BITRATE = {
    (4608, 2592): (4000, 8000),
    (2304, 1296): (1500, 3500),
    (4056, 3040): (3000, 7000),
    (1920, 1080): (1000, 2500),
    (1280, 720):  (500,  1500),
}
TL_VIDEO_BITRATE_DEFAULT = (800, 3000)

# ------------------------------------------------------------
# IMX500 – IA
# ------------------------------------------------------------
IMX500_DEFAULT_CONFIDENCE = 0.5

# ------------------------------------------------------------
# Caméra active sur port 0
# ------------------------------------------------------------
CAM0_CHOICES = ["IMX219", "IMX708"]

# ------------------------------------------------------------
# Utilitaires
# ------------------------------------------------------------
def timestamp():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")

def run_cmd(cmd, cwd=None):
    print(f"[Commande exécutée] : {' '.join(str(c) for c in cmd)}")
    return subprocess.Popen(cmd, cwd=cwd)

def clamp(val, lo, hi):
    try:
        return max(lo, min(hi, val))
    except TypeError:
        return lo

# ------------------------------------------------------------
# Nettoyage IMX500 / libcamera
# ------------------------------------------------------------
def cleanup_imx500():
    subprocess.call(["pkill", "-f", "rpicam"])
    subprocess.call(["pkill", "-f", "imx500"])
    subprocess.call(["pkill", "-f", "python.*imx500"])

def is_imx500_busy():
    out = subprocess.run(["lsof", "/dev/media4"], capture_output=True, text=True)
    return len(out.stdout.strip()) > 0

# ------------------------------------------------------------
# Lecture température CPU (Pi5)
# ------------------------------------------------------------
def get_cpu_temp() -> Optional[float]:
    paths = [
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/class/hwmon/hwmon0/temp1_input",
    ]
    for p in paths:
        try:
            with open(p) as f:
                return int(f.read().strip()) / 1000.0
        except Exception:
            pass
    try:
        r = subprocess.run(["vcgencmd", "measure_temp"],
                           capture_output=True, text=True, timeout=2)
        val = r.stdout.strip()  
        return float(val.replace("temp=", "").replace("'C", ""))
    except Exception:
        return None

# ============================================================
# Contrôleur Caméras
# ============================================================
class CameraController:
    def __init__(self, save_dir: str):
        self.save_dir = save_dir

    @staticmethod                         
    def _append_if_not_none(cmd, flag, value):
        if value is not None:
            cmd += [flag, str(value)]

    def _apply_rotation_pil(self, fichier, rot_angle, hw_rotation, quality):
        pil_angle = (rot_angle - hw_rotation) % 360
        if pil_angle != 0 and os.path.exists(fichier):
            try:
                img_rot = Image.open(fichier)
                img_rot = img_rot.rotate(pil_angle, expand=True)
                q = quality if quality is not None else 95
                img_rot.save(fichier, "JPEG", quality=q)
                print(f"[Rotation PIL] {pil_angle}° appliquée sur {fichier}")
            except Exception as e:
                print(f"[Rotation PIL] Erreur : {e}")

    # ----------------------------------------------------------
    # Helpers communs exposition / AWB
    # ----------------------------------------------------------
    @staticmethod
    def _add_exposition_awb(cmd, shutter_us, gain, awb_mode, awb_gains):
        if shutter_us is not None and shutter_us > 0:
            cmd += ["--shutter", str(int(shutter_us))]
        if gain is not None and gain > 0:
            cmd += ["--gain", f"{gain:.2f}"]
        if awb_mode == "manual" and awb_gains is not None:
            r, b = awb_gains
            cmd += ["--awb", "off", "--awbgains", f"{r:.2f},{b:.2f}"]
        elif awb_mode not in ("auto", ""):
            cmd += ["--awb", awb_mode]

    # ----------------------------------------------------------
    # Méthode commune pour les captures cam0 (IMX219 / IMX708)
    # ----------------------------------------------------------
    def _prendre_photo_cam0(
        self,
        cmd_base: list,
        fichier: str,
        rotation: Optional[float],
        brightness: Optional[float],
        contrast: Optional[float],
        saturation: Optional[float],
        quality: Optional[int],
        shutter_us: Optional[int],
        gain: Optional[float],
        awb_mode: str,
        awb_gains: Optional[tuple],
    ) -> Optional[str]:
        """Lance rpicam-still avec les paramètres communs.
        Retourne le chemin du fichier si OK, None sinon."""
        rot_angle  = clamp(float(rotation),   0,   359) if rotation   is not None else 0
        brightness = clamp(float(brightness), -1.0, 1.0) if brightness is not None else None
        contrast   = clamp(float(contrast),   0.0, 32.0) if contrast   is not None else None
        saturation = clamp(float(saturation), 0.0, 32.0) if saturation is not None else None
        quality    = clamp(int(quality),      1,   100)  if quality    is not None else None

        hw_rotation = 180 if 135 <= rot_angle < 225 else 0
        if hw_rotation:
            cmd_base += ["--rotation", str(hw_rotation)]

        self._append_if_not_none(cmd_base, "--brightness", brightness)
        self._append_if_not_none(cmd_base, "--contrast",   contrast)
        self._append_if_not_none(cmd_base, "--saturation", saturation)
        self._append_if_not_none(cmd_base, "--quality",    quality)
        self._add_exposition_awb(cmd_base, shutter_us, gain, awb_mode, awb_gains)

        result = subprocess.run(cmd_base, capture_output=True)

        if not os.path.exists(fichier) or os.path.getsize(fichier) < 1000:
            print(f"[Erreur capture] Fichier absent ou vide : {fichier} "
                  f"(returncode={result.returncode})")
            return None

        self._apply_rotation_pil(fichier, rot_angle, hw_rotation, quality)
        return fichier

    # ----------------------------------------------------------
    # IMX219
    # ----------------------------------------------------------
    def prendre_photo_imx219(
        self,
        resolution: str = "large",
        nom: Optional[str] = None,
        rotation: Optional[float] = None,
        brightness: Optional[float] = None,
        contrast: Optional[float] = None,
        saturation: Optional[float] = None,
        quality: Optional[int] = None,
        shutter_us: Optional[int] = None,
        gain: Optional[float] = None,
        awb_mode: str = "auto",
        awb_gains: Optional[tuple] = None,
    ) -> Optional[str]:
        if not nom:
            nom = f"photo_IMX219_{timestamp()}"
        fichier = os.path.join(self.save_dir, nom + ".jpg")

        if resolution == "serre":
            cmd = ["rpicam-still", "-o", fichier,
                   "--width", "1920", "--height", "1080", "--camera", "0"]
        else:
            cmd = ["rpicam-still", "-o", fichier,
                   "--width", "3280", "--height", "2464", "--camera", "0"]

        return self._prendre_photo_cam0(
            cmd, fichier, rotation, brightness, contrast,
            saturation, quality, shutter_us, gain, awb_mode, awb_gains)

    def rafale_imx219(
        self,
        n: int,
        delai_s: float,
        resolution: str = "large",
        nom_base: Optional[str] = None,
        rotation: Optional[float] = None,
        brightness: Optional[float] = None,
        contrast: Optional[float] = None,
        saturation: Optional[float] = None,
        quality: Optional[int] = None,
        shutter_us: Optional[int] = None,
        gain: Optional[float] = None,
        awb_mode: str = "auto",
        awb_gains: Optional[tuple] = None,
        log_cb=None,
    ):
        ts_base  = timestamp()
        fichiers = []
        for i in range(n):
            nom = f"{nom_base or 'rafale_IMX219'}_{ts_base}_{i+1:03d}"
            if log_cb:
                log_cb(f"Rafale {i+1}/{n} : {nom}.jpg")
            f = self.prendre_photo_imx219(
                resolution=resolution, nom=nom,
                rotation=rotation, brightness=brightness,
                contrast=contrast, saturation=saturation, quality=quality,
                shutter_us=shutter_us, gain=gain,
                awb_mode=awb_mode, awb_gains=awb_gains,
            )
            if f:
                fichiers.append(f)
            else:
                if log_cb:
                    log_cb(f"⚠ Échec capture {i+1}/{n} – photo ignorée.")
            if i < n - 1 and delai_s > 0:
                time.sleep(delai_s)
        if log_cb:
            log_cb(f"✅ Rafale terminée : {len(fichiers)}/{n} photos réussies.")
        return fichiers

    # ----------------------------------------------------------
    # IMX708 Wide
    # ----------------------------------------------------------
    def prendre_photo_imx708(
        self,
        resolution: str = "large",
        nom: Optional[str] = None,
        rotation: Optional[float] = None,
        brightness: Optional[float] = None,
        contrast: Optional[float] = None,
        saturation: Optional[float] = None,
        quality: Optional[int] = None,
        hdr: bool = False,
        lens_position: Optional[float] = None,
        shutter_us: Optional[int] = None,
        gain: Optional[float] = None,
        awb_mode: str = "auto",
        awb_gains: Optional[tuple] = None,
    ) -> Optional[str]:
        if not nom:
            nom = f"photo_IMX708_{timestamp()}"
        fichier = os.path.join(self.save_dir, nom + ".jpg")

        if resolution == "hd":
            cmd = ["rpicam-still", "-o", fichier,
                   "--width", "2304", "--height", "1296", "--camera", "0"]
        else:
            cmd = ["rpicam-still", "-o", fichier,
                   "--width", "4608", "--height", "2592", "--camera", "0"]

        if hdr:
            cmd += ["--hdr"]

        if lens_position is not None:
            lp = clamp(float(lens_position), IMX708_LENS_MIN, IMX708_LENS_MAX)
            cmd += ["--lens-position", f"{lp:.1f}"]

        return self._prendre_photo_cam0(
            cmd, fichier, rotation, brightness, contrast,
            saturation, quality, shutter_us, gain, awb_mode, awb_gains)

    def rafale_imx708(
        self,
        n: int,
        delai_s: float,
        resolution: str = "large",
        nom_base: Optional[str] = None,
        rotation: Optional[float] = None,
        brightness: Optional[float] = None,
        contrast: Optional[float] = None,
        saturation: Optional[float] = None,
        quality: Optional[int] = None,
        hdr: bool = False,
        lens_position: Optional[float] = None,
        shutter_us: Optional[int] = None,
        gain: Optional[float] = None,
        awb_mode: str = "auto",
        awb_gains: Optional[tuple] = None,
        log_cb=None,
    ):
        ts_base  = timestamp()
        fichiers = []
        for i in range(n):
            nom = f"{nom_base or 'rafale_IMX708'}_{ts_base}_{i+1:03d}"
            if log_cb:
                log_cb(f"Rafale {i+1}/{n} : {nom}.jpg")
            f = self.prendre_photo_imx708(
                resolution=resolution, nom=nom,
                rotation=rotation, brightness=brightness,
                contrast=contrast, saturation=saturation, quality=quality,
                hdr=hdr, lens_position=lens_position,
                shutter_us=shutter_us, gain=gain,
                awb_mode=awb_mode, awb_gains=awb_gains,
            )
            if f:
                fichiers.append(f)
            else:
                if log_cb:
                    log_cb(f"⚠ Échec capture {i+1}/{n} – photo ignorée.")
            if i < n - 1 and delai_s > 0:
                time.sleep(delai_s)
        if log_cb:
            log_cb(f"✅ Rafale terminée : {len(fichiers)}/{n} photos réussies.")
        return fichiers

    # ----------------------------------------------------------
    # IMX500
    # ----------------------------------------------------------
    def prendre_photo_imx500(self, nom: Optional[str] = None) -> Optional[str]:
        if not nom:
            nom = f"photo_IMX500_{timestamp()}"
        fichier = os.path.join(self.save_dir, nom + ".jpg")
        cmd = ["rpicam-still", "-o", fichier, "--camera", "1"]
        result = subprocess.run(cmd, capture_output=True)
        if not os.path.exists(fichier) or os.path.getsize(fichier) < 1000:
            print(f"[Erreur capture IMX500] Fichier absent ou vide : {fichier} "
                  f"(returncode={result.returncode})")
            return None
        return fichier

    # ----------------------------------------------------------
    # Vidéo générique / HFR
    # ----------------------------------------------------------
    def faire_video(self, camera: str, label: str, duree_s: int = 10,
                    nom: Optional[str] = None):
        duree_ms = 0 if duree_s <= 0 else duree_s * 1000
        if duree_ms == 0:
            cmd = ["rpicam-vid", "-t", "0", "--camera", camera]
            return run_cmd(cmd)
        if not nom:
            nom = f"video_{label}_{timestamp()}"
        fichier = os.path.join(self.save_dir, nom + ".mp4")
        cmd = ["rpicam-vid", "-t", str(duree_ms), "-o", fichier, "--camera", camera]
        subprocess.run(cmd)
        return fichier

    def capturer_hfr(self, width: int, height: int, fps: int, duree_s: int,
                     nom: Optional[str] = None) -> str:
        if not nom:
            nom = f"HFR_{fps}fps_{timestamp()}"
        fichier  = os.path.join(self.save_dir, nom + ".mp4")
        duree_ms = duree_s * 1000
        cmd = [
            "rpicam-vid",
            "-t",          str(duree_ms),
            "-o",          fichier,
            "--camera",    "0",
            "--width",     str(width),
            "--height",    str(height),
            "--framerate", str(fps),
        ]
        subprocess.run(cmd)
        return fichier

# ============================================================
# Contrôleur IA IMX500
# ============================================================
class IAController:
    def __init__(self, save_dir: str, demos_path: str):
        self.save_dir      = save_dir
        self.demos_path    = demos_path
        self.log_ia_active = False
        self.log_ia_path   = None
        self.log_ia_format = "csv"
        self._log_lock     = threading.Lock()
        self._json_buffer  = []

    def _get_postprocess_with_confidence(self, base_json: str,
                                         confidence: float) -> str:
        """Génère un JSON temporaire avec le seuil de confiance personnalisé."""
        if abs(confidence - 0.5) < 0.01:
            return base_json
        try:
            with open(base_json, "r") as f:
                data = json.load(f)
            for stage in data.get("post_process_stages", []):
                params = stage.setdefault("params", {})
                if "object_detect" in stage.get("name", ""):
                    params["confidence_threshold"] = confidence
                elif "confidence_threshold" in params:
                    params["confidence_threshold"] = confidence
            tmp = os.path.join(
                "/tmp",
                f"imx500_conf{int(confidence*100):03d}_{os.path.basename(base_json)}"
            )
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2)
            return tmp
        except Exception:
            return base_json

    # ----------------------------------------------------------
    # Log détections - hors "Détection Objets" et "PoseNet" - rpicam-app
    # ----------------------------------------------------------
    def start_log(self, path: str, fmt: str = "csv"):
        self.log_ia_path   = path
        self.log_ia_format = fmt
        self.log_ia_active = True
        self._json_buffer  = []
        if fmt == "csv":
            try:
                with open(path, "w", newline="") as f:
                    csv.writer(f).writerow(
                        ["timestamp", "label", "confidence", "bbox"])
            except Exception:
                pass

    def stop_log(self):
        self.log_ia_active = False

    def log_detection(self, label: str, confidence: float, bbox=None):
        if not self.log_ia_active or not self.log_ia_path:
            return
        ts = datetime.datetime.now().isoformat(timespec="seconds")
        with self._log_lock:
            if self.log_ia_format == "csv":
                try:
                    with open(self.log_ia_path, "a", newline="") as f:
                        csv.writer(f).writerow(
                            [ts, label, f"{confidence:.3f}",
                             str(bbox) if bbox else ""])
                except Exception:
                    pass
            else:
                entry = {"ts": ts, "label": label,
                         "confidence": round(confidence, 3)}
                if bbox:
                    entry["bbox"] = bbox
                self._json_buffer.append(entry)
                try:
                    with open(self.log_ia_path, "w") as f:
                        json.dump(self._json_buffer, f, indent=2)
                except Exception:
                    pass

    # ----------------------------------------------------------
    # Modes IA
    # ----------------------------------------------------------
    def _continu(self, cmd):
        cleanup_imx500()
        env = os.environ.copy()
        if self.log_ia_active and self.log_ia_path:
            env["IMX500_LOG_CSV"] = self.log_ia_path
        print(f"[Commande exécutée] : {' '.join(str(c) for c in cmd)}")
        return subprocess.Popen(cmd, cwd=self.demos_path, env=env)

    def ia_detection_objets(self, confidence: float = 0.5):
        base = "/usr/share/rpi-camera-assets/imx500_mobilenet_ssd.json"
        pp   = self._get_postprocess_with_confidence(base, confidence)
        return self._continu([
            "rpicam-hello", "-t", "0",
            "--post-process-file", pp, "--camera", "1"
        ])

    def ia_pose(self):
        return self._continu([
            "rpicam-hello", "-t", "0",
            "--post-process-file",
            "/usr/share/rpi-camera-assets/imx500_posenet.json",
            "--camera", "1"
        ])

    def ia_higherhrnet(self):
        return self._continu([
            "python3",
            os.path.join(self.demos_path,
                         "imx500_pose_estimation_higherhrnet_demo.py")
        ])

    def ia_segmentation(self):
        return self._continu([
            "python3",
            os.path.join(self.demos_path, "imx500_segmentation_demo.py")
        ])

    def ia_classification(self):
        return self._continu([
            "python3",
            os.path.join(self.demos_path, "imx500_classification_demo.py")
        ])

    def ia_detection_rapide(self):
        return self._continu([
            "python3",
            os.path.join(self.demos_path,
                         "imx500_object_detection_demo_mp.py")
        ])

    def ia_video(self, duree_s: int = 10, nom: Optional[str] = None,
                 confidence: float = 0.5):
        cleanup_imx500()
        duree_ms = duree_s * 1000
        if not nom:
            nom = f"videoIA_IMX500_{timestamp()}"
        fichier = os.path.join(self.save_dir, nom + ".mp4")
        base    = "/usr/share/rpi-camera-assets/imx500_mobilenet_ssd.json"
        pp      = self._get_postprocess_with_confidence(base, confidence)
        cmd = [
            "rpicam-vid", "-t", str(duree_ms), "-o", fichier,
            "--post-process-file", pp, "--camera", "1"
        ]
        subprocess.run(cmd, cwd=self.demos_path)
        return fichier

# ============================================================
# Contrôleur Timelapse
# ============================================================
class TimelapseController:
    """Gère la capture timelapse pour IMX708 (port 0) et IMX500 (port 1).
    Compléments :
      - AWB lock + AE lock pour éviter le scintillement entre frames
      - Reprise de session via resume_state.json
      - Watchdog : arrêt auto si TL_MAX_ERREURS_CONSECUTIVES captures échouent
      - Estimations statiques de taille disque (frames + vidéo MP4)"""

    RESUME_FILE = "resume_state.json"

    def __init__(self, save_dir: str):
        self.save_dir  = save_dir
        self.capturing = False
        self._thread   = None
        self._stop_event = threading.Event()

    # ----------------------------------------------------------
    # Persistance de session
    # ----------------------------------------------------------
    @staticmethod
    def _save_resume(output_dir: str, state: dict):
        try:
            with open(os.path.join(output_dir,
                                   TimelapseController.RESUME_FILE), "w") as f:
                json.dump(state, f, indent=2)
        except Exception:
            pass

    @staticmethod
    def _load_resume(output_dir: str) -> Optional[dict]:
        try:
            p = os.path.join(output_dir, TimelapseController.RESUME_FILE)
            if os.path.exists(p):
                with open(p) as f:
                    return json.load(f)
        except Exception:
            pass
        return None

    @staticmethod
    def _clear_resume(output_dir: str):
        try:
            p = os.path.join(output_dir, TimelapseController.RESUME_FILE)
            if os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

    @staticmethod
    def has_resume(output_dir: str) -> bool:
        return os.path.exists(
            os.path.join(output_dir, TimelapseController.RESUME_FILE))

    @staticmethod
    def get_resume_info(output_dir: str) -> Optional[dict]:
        return TimelapseController._load_resume(output_dir)

    # ----------------------------------------------------------
    # Démarrage / Arrêt
    # ----------------------------------------------------------
    def demarrer(
        self,
        camera: str,
        interval_s: int,
        duration_min: int,
        width: int,
        height: int,
        output_dir: str,
        hdr: bool = False,
        awb_lock: bool = False,
        awb_gains: Optional[tuple] = None,
        resume: bool = False,
        log_cb=None,
    ):
        if self.capturing:
            return
        os.makedirs(output_dir, exist_ok=True)
        self._stop_event.clear()          
        self.capturing = True
        self._thread   = threading.Thread(
            target=self._loop,
            args=(camera, interval_s, duration_min, width, height,
                  output_dir, hdr, awb_lock, awb_gains, resume, log_cb),
            daemon=True,
        )
        self._thread.start()

    def arreter(self):
        self.capturing = False
        self._stop_event.set()         

    # ----------------------------------------------------------
    # Boucle principale avec Watchdog
    # ----------------------------------------------------------
    def _loop(self, camera, interval_s, duration_min, width, height,
              output_dir, hdr, awb_lock, awb_gains, resume, log_cb):
        cam_index    = "1" if camera == "IMX500" else "0"
        duration_sec = duration_min * 60
        erreurs_cons = 0
        frame_index  = 0
        start_time   = time.time()

        # Reprise de session
        if resume:
            state = self._load_resume(output_dir)
            if state:
                elapsed     = state.get("elapsed_s", 0)
                frame_index = state.get("frame_index", 0)
                start_time  = time.time() - elapsed
                if log_cb:
                    log_cb(f"▶ Reprise depuis frame {frame_index + 1} "
                           f"({elapsed:.0f} s déjà écoulées).")

        while self.capturing and (time.time() - start_time) < duration_sec:
            ts       = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = os.path.join(output_dir, f"tl_{camera}_{ts}.jpg")

            cmd = [
                "rpicam-still",
                "-o", filename,
                "--width",  str(width),
                "--height", str(height),
                "--camera", cam_index,
                "-n",
                "--timeout", "200",
            ]
            if hdr and camera == "IMX708":
                cmd += ["--hdr"]

            # AWB lock anti-scintillement
            if awb_lock and camera in ("IMX708", "IMX219"):
                if awb_gains and len(awb_gains) == 2:
                    r, b = awb_gains
                    cmd += ["--awb", "off", "--awbgains", f"{r:.2f},{b:.2f}"]
                else:
                    cmd += ["--awb", "off"]

            if log_cb:
                log_cb(f"Capture {frame_index + 1} : "
                       f"{os.path.basename(filename)}")

            t0_capture = time.time()
            subprocess.run(cmd, capture_output=True)
            duree_capture = time.time() - t0_capture

            # Watchdog : vérifie que le fichier a bien été écrit
            ok = os.path.exists(filename) and os.path.getsize(filename) > 1000
            if not ok:
                erreurs_cons += 1
                if log_cb:
                    log_cb(f"⚠ Erreur capture ({erreurs_cons}/"
                           f"{TL_MAX_ERREURS_CONSECUTIVES})")
                if erreurs_cons >= TL_MAX_ERREURS_CONSECUTIVES:
                    if log_cb:
                        log_cb("❌ Watchdog : trop d'erreurs consécutives. "
                               "Timelapse arrêté automatiquement.")
                    self.capturing = False
                    break
            else:
                erreurs_cons  = 0
                frame_index  += 1
                self._save_resume(output_dir, {
                    "camera": camera, "interval_s": interval_s,
                    "duration_min": duration_min,
                    "width": width, "height": height,
                    "hdr": hdr, "awb_lock": awb_lock,
                    "awb_gains": list(awb_gains) if awb_gains else None,
                    "elapsed_s":   round(time.time() - start_time, 1),
                    "frame_index": frame_index,
                })

            attente = max(0, interval_s - duree_capture)
            for _ in range(int(attente)):
                if self._stop_event.wait(timeout=1):
                    break

        self.capturing = False
        self._clear_resume(output_dir)
        if log_cb:
            log_cb(f"Timelapse terminé. {frame_index} frame(s) capturée(s).")

    # ----------------------------------------------------------
    # Assemblage vidéo MP4
    # ----------------------------------------------------------
    @staticmethod
    def creer_video(output_dir: str, fps: int, video_path: str, log_cb=None):
        import tempfile

        # Construire la liste triée des frames
        try:
            frames = sorted(
                f for f in os.listdir(output_dir)
                if f.startswith("tl_") and f.endswith(".jpg")
            )
        except Exception as e:
            if log_cb:
                log_cb(f"❌ Impossible de lister les frames : {e}")
            return

        if not frames:
            if log_cb:
                log_cb("❌ Aucune frame tl_*.jpg trouvée dans le dossier.")
            return

        # Écrire le fichier de liste temporaire
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False
            ) as liste_f:
                liste_path = liste_f.name
                for frame in frames:
                    safe = os.path.join(output_dir, frame).replace("'", "'\\''")
                    liste_f.write(f"file '{safe}'\n")
        except Exception as e:
            if log_cb:
                log_cb(f"❌ Impossible d'écrire le fichier de liste : {e}")
            return

        cmd = [
            "ffmpeg", "-y",
            "-f",        "concat",
            "-safe",     "0",
            "-r",        str(fps),
            "-i",        liste_path,
            "-c:v",      "libx264",
            "-pix_fmt",  "yuv420p",
            video_path,
        ]
        if log_cb:
            log_cb(f"Assemblage de {len(frames)} frames en vidéo MP4 ({fps} fps)…")
        result = subprocess.run(cmd, capture_output=True, text=True)
        try:
            os.remove(liste_path)
        except Exception:
            pass
        if result.returncode == 0:
            if log_cb:
                log_cb(f"✅ Vidéo créée : {video_path}")
        else:
            if log_cb:
                log_cb(f"❌ Erreur ffmpeg : {result.stderr[-200:]}")

    # ----------------------------------------------------------
    # Estimations taille disque
    # ----------------------------------------------------------
    @staticmethod
    def estimer_taille(width: int, height: int, nb_frames: int,
                       fps_video: int) -> dict:
        key = (width, height)
        ko_min, ko_max = TL_JPEG_SIZE_TABLE.get(key, TL_JPEG_DEFAULT_SIZE)
        frames_mo_min  = nb_frames * ko_min / 1024
        frames_mo_max  = nb_frames * ko_max / 1024

        duree_vid_s    = nb_frames / max(1, fps_video)
        br_min, br_max = TL_VIDEO_BITRATE.get(key, TL_VIDEO_BITRATE_DEFAULT)
        video_mo_min   = duree_vid_s * br_min / 8 / 1024
        video_mo_max   = duree_vid_s * br_max / 8 / 1024

        return {
            "nb_frames":     nb_frames,
            "duree_vid_s":   duree_vid_s,
            "frames_mo_min": frames_mo_min,
            "frames_mo_max": frames_mo_max,
            "video_mo_min":  video_mo_min,
            "video_mo_max":  video_mo_max,
            "total_mo_min":  frames_mo_min + video_mo_min,
            "total_mo_max":  frames_mo_max + video_mo_max,
        }

# ============================================================
# Contrôleur Slow Motion
# ============================================================
class SlowMotionController:

    @staticmethod
    def ouvrir_vlc(fichier: str):
        try:
            subprocess.Popen(["vlc", fichier])
        except FileNotFoundError:
            subprocess.Popen(["xdg-open", fichier])

    @staticmethod
    def convertir_setpts(source: str, output: str, source_fps: int,
                         target_fps: int, log_cb=None):
        facteur  = source_fps / target_fps
        pts_expr = f"PTS*{facteur:.4f}"
        cmd = [
            "ffmpeg", "-y",
            "-i",      source,
            "-vf",     f"setpts={pts_expr}",
            "-r",      str(target_fps),
            "-c:v",    "libx264",
            "-pix_fmt","yuv420p",
            "-an",     output,
        ]
        if log_cb:
            log_cb(f"[setpts] × {facteur:.1f} → {os.path.basename(output)}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            if log_cb:
                log_cb(f"✅ Vidéo ralenti créée : {output}")
        else:
            if log_cb:
                log_cb(f"❌ Erreur ffmpeg : {result.stderr[-300:]}")

    @staticmethod
    def convertir_minterpolate(source: str, output: str, source_fps: int,
                               target_fps: int, log_cb=None):
        facteur    = source_fps / target_fps
        interp_fps = source_fps
        pts_expr   = f"PTS*{facteur:.4f}"
        cmd = [
            "ffmpeg", "-y",
            "-i",   source,
            "-vf",  f"minterpolate=fps={interp_fps}:mi_mode=mci,"
                    f"setpts={pts_expr}",
            "-r",   str(target_fps),
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-an",  output,
        ]
        if log_cb:
            log_cb(f"[minterpolate] × {facteur:.1f} "
                   f"→ {os.path.basename(output)}")
            log_cb("⏳ Cette méthode peut prendre plusieurs minutes…")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            if log_cb:
                log_cb(f"✅ Vidéo ralenti interpolée : {output}")
        else:
            if log_cb:
                log_cb(f"❌ Erreur ffmpeg : {result.stderr[-300:]}")

    @staticmethod
    def detecter_fps(fichier: str) -> Optional[float]:
        try:
            r = subprocess.run(
                ["ffprobe", "-v", "0",
                 "-select_streams", "v:0",
                 "-show_entries", "stream=r_frame_rate",
                 "-of", "default=noprint_wrappers=1:nokey=1",
                 fichier],
                capture_output=True, text=True, timeout=10,
            )
            val = r.stdout.strip()
            if "/" in val:
                num, den = val.split("/")
                return float(num) / float(den)
            return float(val)
        except Exception:
            return None

# ============================================================
# Contrôleur Motion
# ============================================================
class MotionController:

    @staticmethod
    def _systemctl(action: str):
        try:
            result = subprocess.run(
                ["sudo", SYSTEMCTL, action, "motion"],
                capture_output=True, text=True,
                timeout=MOTION_SYSTEMCTL_TIMEOUT,
            )
            ok  = result.returncode == 0
            msg = result.stdout.strip() or result.stderr.strip()
            return ok, msg
        except subprocess.TimeoutExpired:
            return False, (
                f"Délai dépassé ({MOTION_SYSTEMCTL_TIMEOUT} s). "
                "Motion est peut-être en cours de démarrage — "
                "cliquez sur « Rafraîchir » dans quelques secondes."
            )
        except Exception as e:
            return False, str(e)

    @staticmethod
    def is_running() -> bool:
        try:
            r = subprocess.run(
                [SYSTEMCTL, "is-active", "motion"],
                capture_output=True, text=True)
            return r.stdout.strip() == "active"
        except Exception:
            return False

    @staticmethod
    def check_libcamerify() -> bool:
        return os.path.isfile(LIBCAMERIFY)

    @staticmethod
    def check_service_patched() -> bool:
        service_file = "/usr/lib/systemd/system/motion.service"
        try:
            with open(service_file) as f:
                return "libcamerify" in f.read()
        except Exception:
            return False

    @staticmethod
    def demarrer():
        return MotionController._systemctl("start")

    @staticmethod
    def arreter_proprement() -> tuple:
        try:
            subprocess.run(["sudo", "pkill", "-15", "-x", "motion"],
                           capture_output=True, timeout=5)
        except Exception:
            pass
        time.sleep(MOTION_STOP_GRACE)
        return MotionController._systemctl("stop")

    @staticmethod
    def redemarrer():
        MotionController.arreter_proprement()
        time.sleep(1)
        return MotionController._systemctl("start")

    @staticmethod
    def ouvrir_interface_web():
        webbrowser.open(MOTION_WEB_CTRL)

    @staticmethod
    def ouvrir_stream():
        webbrowser.open(MOTION_WEB_STREAM)

    @staticmethod
    def reparer_videos(repertoire: str, log_cb=None) -> int:
        nb = 0
        extensions = (".mp4", ".mkv", ".avi")
        try:
            fichiers = [
                f for f in os.listdir(repertoire)
                if os.path.splitext(f)[1].lower() in extensions
            ]
        except Exception as e:
            if log_cb:
                log_cb(f"❌ Impossible de lister le répertoire : {e}")
            return 0

        for nom in fichiers:
            src  = os.path.join(repertoire, nom)
            base, ext = os.path.splitext(nom)
            dst  = os.path.join(repertoire, f"{base}_repare{ext}")
            if log_cb:
                log_cb(f"Réparation : {nom} …")
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", src, "-c", "copy", dst],
                capture_output=True, text=True,
            )
            if result.returncode == 0 and os.path.exists(dst):
                try:
                    os.replace(dst, src)
                    if log_cb:
                        log_cb(f"   ✅ {nom} réparé.")
                    nb += 1
                except Exception as e:
                    if log_cb:
                        log_cb(f"   ⚠ Remplacement échoué : {e}")
            else:
                if log_cb:
                    log_cb(f"   ❌ Impossible de réparer {nom}.")
                try:
                    os.remove(dst)
                except Exception:
                    pass
        return nb

    @staticmethod
    def get_config_path() -> str:
        for p in ["/etc/motion/motion.conf",
                  os.path.expanduser("~/.motion/motion.conf")]:
            if os.path.exists(p):
                return p
        return "/etc/motion/motion.conf"

    @staticmethod
    def get_capture_dir() -> Optional[str]:
        conf = MotionController.get_config_path()
        try:
            with open(conf) as f:
                for line in f:
                    line = line.strip()
                    if (line.startswith("target_dir")
                            and not line.startswith("#")):
                        parts = line.split(None, 1)
                        if len(parts) == 2:
                            return parts[1].strip()
        except Exception:
            pass
        return None

    @staticmethod
    def set_movie_output(activer: bool) -> bool:
        """Définit le mode Motion via motion_mode_manager (verrou inter-processus).
        activer=True  → mode 'record'  (movie_output on)
        activer=False → mode 'stream'  (movie_output off)"""
        try:
            mode = "record" if activer else "stream"
            return set_mode(mode, caller=os.path.basename(__file__))
        except Exception as e:
            print(f"[MotionController.set_movie_output] Erreur : {e}")
            return False

    @staticmethod
    def get_movie_output() -> bool:
        """Retourne True si le mode courant est 'record' (movie_output on)."""
        try:
            return get_mode() == "record"
        except Exception:
            return True  # défaut conservateur

# ============================================================
# Interface graphique Tkinter
# ============================================================
class CameraGUI:
    def __init__(self, root, camera_ctrl: CameraController,
                 ia_ctrl: IAController):
        self.root            = root
        self.camera_ctrl     = camera_ctrl
        self.ia_ctrl         = ia_ctrl
        self.timelapse_ctrl  = TimelapseController(camera_ctrl.save_dir)
        self.sm_ctrl         = SlowMotionController()
        self.process_continu = None
        self._preview_process = None

        self.var_cam0 = tk.StringVar(value="IMX708")
        self.root.bind_all("<Return>", self._validate_dialog)

        def load_resized(path, max_w, max_h):
            img = Image.open(path)
            img.thumbnail((max_w, max_h), Image.LANCZOS)
            return ImageTk.PhotoImage(img)

        self.img_ventilo   = load_resized(
            os.path.join(ICON_DIR, "RPI5_ventilateur.png"), 200, 200)
        self.img_camera219 = load_resized(
            os.path.join(ICON_DIR, "IMX219.png"), 210, 210)
        self.img_camera708 = load_resized(
            os.path.join(ICON_DIR, "IMX708wide.png"), 200, 200)
        self.img_imx500    = load_resized(
            os.path.join(ICON_DIR, "RPI5_imx500.png"), 280, 280)
        try:
            self.img_timelapse = load_resized(
                os.path.join(ICON_DIR, "timelapse.png"), 80, 80)
        except Exception:
            self.img_timelapse = None
        try:
            self.img_slowmo = load_resized(
                os.path.join(ICON_DIR, "slow-motion.png"), 300, 300)
        except Exception:
            self.img_slowmo = None

        self.root.title(
            "Pilotage Caméras Sony Raspberry Pi 5 – IMX219 / IMX708 / IMX500")
        # self.root.geometry("1400x850")

        # ── Réglages photo communs ──────────────────────────────
        self.var_rotation   = tk.DoubleVar(value=0)
        self.var_brightness = tk.DoubleVar(value=0)
        self.var_contrast   = tk.DoubleVar(value=1)
        self.var_saturation = tk.DoubleVar(value=1)
        self.var_quality    = tk.IntVar(value=95)
        self.var_hdr        = tk.BooleanVar(value=False)
        self.var_lens       = tk.DoubleVar(value=IMX708_LENS_INFINITY)

        # ── Exposition manuelle ─────────────────────────────────
        self.var_shutter_auto = tk.BooleanVar(value=True)
        self.var_shutter_us   = tk.IntVar(value=10000)
        self.var_gain_auto    = tk.BooleanVar(value=True)
        self.var_gain         = tk.DoubleVar(value=1.0)

        # ── Balance des blancs ──────────────────────────────────
        self.var_awb_mode = tk.StringVar(value="auto")
        self.var_awb_r    = tk.DoubleVar(value=2.0)
        self.var_awb_b    = tk.DoubleVar(value=1.8)

        # ── Rafale ──────────────────────────────────────────────
        self.var_burst_n     = tk.IntVar(value=3)
        self.var_burst_delay = tk.DoubleVar(value=1.0)

        # ── Miniatures dernière photo ────────────────────────────
        self._last_photo_219: Optional[ImageTk.PhotoImage] = None
        self._last_photo_708: Optional[ImageTk.PhotoImage] = None
        self._thumb_label_219 = None
        self._thumb_label_708 = None

        # ── IMX500 seuil confiance ───────────────────────────────
        self.var_ia_confidence = tk.DoubleVar(value=IMX500_DEFAULT_CONFIDENCE)

        # ── IMX500 log IA ────────────────────────────────────────
        self.var_ia_log_active = tk.BooleanVar(value=False)
        self.var_ia_log_fmt    = tk.StringVar(value="csv")
        self.var_ia_log_path   = tk.StringVar(
            value=os.path.join(DEFAULT_SAVE_DIR, "ia_detections.csv"))
        
        root.minsize(900, 650)
        root.geometry("980x720")
        self._build_ui()

    # ----------------------------------------------------------
    # Dialogue personnalisé
    # ----------------------------------------------------------
    def askstring_kp(self, title, message):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.grab_set()
        ttk.Label(dialog, text=message).pack(padx=20, pady=10)
        entry = ttk.Entry(dialog)
        entry.pack(padx=20, pady=10)
        entry.focus_set()
        result = {"value": None}

        def validate():
            result["value"] = entry.get()
            dialog.destroy()

        def cancel():
            result["value"] = None
            dialog.destroy()

        frame = ttk.Frame(dialog)
        frame.pack(pady=10)
        ttk.Button(frame, text="OK",      command=validate).pack(
            side="left", padx=5)
        ttk.Button(frame, text="Annuler", command=cancel).pack(
            side="left", padx=5)
        dialog.bind("<Return>",   lambda e: validate())
        dialog.bind("<KP_Enter>", lambda e: validate())
        dialog.wait_window()
        return result["value"]

    def _validate_dialog(self, event=None):
        widget = self.root.focus_get()
        parent = widget
        dialog = None
        while parent:
            if isinstance(parent, tk.Toplevel) and parent is not self.root:
                dialog = parent
                break
            parent = parent.master
        if dialog is None:
            return
        if isinstance(widget, tk.Entry):
            return
        for child in dialog.winfo_children():
            if (isinstance(child, ttk.Button)
                    and child.cget("text").lower().startswith("ok")):
                child.invoke()
                return

    # ----------------------------------------------------------
    # Popups
    # ----------------------------------------------------------
    def popup_info(self, titre, message):
        fen = tk.Toplevel(self.root)
        fen.title(titre)
        fen.geometry("+200+700")
        ttk.Label(fen, text=message, padding=10).pack()
        ttk.Button(fen, text="OK", command=fen.destroy).pack(pady=5)

    def popup_attente(self, message="Chargement en cours..."):
        fen = tk.Toplevel(self.root)
        fen.title("Veuillez patienter")
        fen.geometry("320x150+400+300")
        fen.resizable(False, False)
        fen.grab_set()
        ttk.Label(fen, text=message, padding=10).pack()
        pb = ttk.Progressbar(fen, mode="indeterminate", length=250)
        pb.pack(pady=10)
        pb.start(10)
        return fen

    # ----------------------------------------------------------
    # Helpers réglages
    # ----------------------------------------------------------
    def _get_rotation(self):
        return clamp(float(self.var_rotation.get()), 0, 359)

    def _get_brightness(self):
        return clamp(float(self.var_brightness.get()), -1.0, 1.0)

    def _get_contrast(self):
        return clamp(float(self.var_contrast.get()), 0.0, 32.0)

    def _get_saturation(self):
        return clamp(float(self.var_saturation.get()), 0.0, 32.0)

    def _get_quality(self):
        return clamp(int(self.var_quality.get()), 1, 100)

    def _get_lens(self):
        return clamp(float(self.var_lens.get()), IMX708_LENS_MIN,
                     IMX708_LENS_MAX)

    def _get_awb(self):
        """Retourne (awb_mode, awb_gains_or_None)."""
        mode = self.var_awb_mode.get()
        if mode == "manual":
            r = clamp(float(self.var_awb_r.get()), 0.1, 8.0)
            b = clamp(float(self.var_awb_b.get()), 0.1, 8.0)
            return "manual", (r, b)
        return mode, None

    def _get_shutter(self):
        if self.var_shutter_auto.get():
            return None
        return clamp(int(self.var_shutter_us.get()), 100, 1000000)

    def _get_gain(self):
        if self.var_gain_auto.get():
            return None
        return clamp(float(self.var_gain.get()), 0.1, 16.0)

    def _reset_reglages(self):
        self.var_rotation.set(0)
        self.var_brightness.set(0.0)
        self.var_contrast.set(1.0)
        self.var_saturation.set(1.0)
        self.var_quality.set(95)
        self.var_hdr.set(False)
        self.var_lens.set(IMX708_LENS_INFINITY)
        self.var_shutter_auto.set(True)
        self.var_shutter_us.set(10000)
        self.var_gain_auto.set(True)
        self.var_gain.set(1.0)
        self.var_awb_mode.set("auto")
        self.var_awb_r.set(2.0)
        self.var_awb_b.set(1.8)

    # ----------------------------------------------------------
    # Barre de statut système (température + disque)
    # ----------------------------------------------------------
    def _build_status_bar(self):
        bar = ttk.Frame(self.root, relief="sunken")
        bar.pack(side="bottom", fill="x")
        self._lbl_temp  = ttk.Label(bar, text="Temp. CPU : --",
                                    font=("Arial", 9))
        self._lbl_temp.pack(side="left", padx=10, pady=2)
        ttk.Separator(bar, orient="vertical").pack(
            side="left", fill="y", pady=2)
        self._lbl_disk  = ttk.Label(bar, text="Disque : --",
                                    font=("Arial", 9))
        self._lbl_disk.pack(side="left", padx=10, pady=2)
        ttk.Separator(bar, orient="vertical").pack(
            side="left", fill="y", pady=2)
        self._lbl_dir   = ttk.Label(bar, text="", font=("Arial", 9),
                                    foreground="#555555")
        self._lbl_dir.pack(side="left", padx=10, pady=2)

    def _status_update_loop(self):
        def _refresh():
            # Température
            t = get_cpu_temp()
            if t is not None:
                color = "#cc0000" if t >= 75 else (
                    "#cc6600" if t >= 65 else "#007700")
                self._lbl_temp.config(
                    text=f"🌡 Temp. CPU : {t:.1f} °C", foreground=color)
            else:
                self._lbl_temp.config(
                    text="Temp. CPU : N/A", foreground="#555555")
            # Espace disque
            try:
                usage = shutil.disk_usage(self.camera_ctrl.save_dir)
                libre_go = usage.free / 1024**3
                total_go = usage.total / 1024**3
                color = "#cc0000" if libre_go < 1 else (
                    "#cc6600" if libre_go < 5 else "#007700")
                self._lbl_disk.config(
                    text=f"💾 Disque libre : {libre_go:.1f} Go / "
                         f"{total_go:.1f} Go",
                    foreground=color)
            except Exception:
                self._lbl_disk.config(
                    text="Disque : N/A", foreground="#555555")
            # Dossier actif
            self._lbl_dir.config(
                text=f"📂 {self.camera_ctrl.save_dir}")
            # Rafraîchissement toutes les 15 s
            self.root.after(15000, _refresh)

        _refresh()

    # ----------------------------------------------------------
    # Bandeau caméra active port 0
    # ----------------------------------------------------------
    def _build_cam0_selector(self, parent):
        frame = ttk.LabelFrame(parent, text="⚠  Port caméra 0 – Nappe connectée")
        frame.pack(fill="x", padx=10, pady=(4, 2))
        row = ttk.Frame(frame)
        row.pack(pady=6)
        ttk.Label(row, text="Caméra connectée :").pack(
            side="left", padx=(8, 12))
        ttk.Radiobutton(row, text="IMX219", variable=self.var_cam0,
                        value="IMX219",
                        command=self._on_cam0_changed).pack(
            side="left", padx=8)
        ttk.Radiobutton(row, text="IMX708", variable=self.var_cam0,
                        value="IMX708",
                        command=self._on_cam0_changed).pack(
            side="left", padx=8)
        self.label_cam0_status = ttk.Label(
            frame, font=("Arial", 10, "bold"), anchor="center")
        self.label_cam0_status.pack(fill="x", padx=10, pady=(0, 6))
        self._on_cam0_changed()

    def _on_cam0_changed(self):
        cam = self.var_cam0.get()
        if cam == "IMX219":
            self.label_cam0_status.config(
                text="● Caméra IMX219 active sur port 0",
                foreground="#0055cc")
        else:
            self.label_cam0_status.config(
                text="● Caméra IMX708 Wide active sur port 0",
                foreground="#007700")

    # ----------------------------------------------------------
    # UI principale
    # ----------------------------------------------------------
    def _build_ui(self):
        top = ttk.Frame(self.root)
        top.pack(fill="x", pady=4)
        ttk.Label(top, text="Dossier de sauvegarde :").pack(
            side="left", padx=5)
        self.label_save_dir = ttk.Label(top,
                                        text=self.camera_ctrl.save_dir)
        self.label_save_dir.pack(side="left", padx=5)
        ttk.Button(top, text="Changer...",
                command=self.changer_repertoire).pack(
            side="left", padx=5)
        ttk.Button(top, text="Arrêter mode continu",
                command=self.arreter_continu).pack(
            side="right", padx=5)

        self._build_cam0_selector(self.root)
        self._build_status_bar()
        self._status_update_loop()

        notebook = ttk.Notebook(self.root)
        notebook.pack(expand=True, fill="both", pady=(4, 0), padx=10)

        tab219        = ttk.Frame(notebook)
        tab708        = ttk.Frame(notebook)
        tab500        = ttk.Frame(notebook)
        tab_motion    = ttk.Frame(notebook)
        tab_timelapse = ttk.Frame(notebook)
        tab_slowmo    = ttk.Frame(notebook)
        tab_fichiers  = ttk.Frame(notebook)

        notebook.add(tab219,        text="IMX219")
        notebook.add(tab708,        text="IMX708 Wide")
        notebook.add(tab500,        text="IMX500  (IA)")
        notebook.add(tab_motion,    text="Motion")
        notebook.add(tab_timelapse, text="⏱ Timelapse")
        notebook.add(tab_slowmo,    text="🎬 Slow Motion")
        notebook.add(tab_fichiers,  text="📁 Fichiers")

        self._build_tab_imx219(tab219)
        self._build_tab_imx708(tab708)
        self._build_tab_imx500(tab500)
        self._build_tab_motion(tab_motion)
        self._build_tab_timelapse(tab_timelapse)
        self._build_tab_slowmo(tab_slowmo)
        self._build_tab_fichiers(tab_fichiers)

        # Rafraîchissement automatique à l'ouverture de l'onglet Fichiers
        def _on_tab_changed(event):
            selected = notebook.tab(notebook.select(), "text")
            if "Fichiers" in selected:
                self._fich_refresh()
        notebook.bind("<<NotebookTabChanged>>", _on_tab_changed)

    # ----------------------------------------------------------
    # Sliders communs + exposition + AWB
    # ----------------------------------------------------------
    def _build_sliders(self, parent, include_lens=False, include_hdr=False):
        def add_slider_row(lbl, from_, to_, variable, fmt=".2f"):
            row = ttk.Frame(parent)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=lbl, width=26).pack(side="left",
                                                    padx=(4, 8))
            ttk.Scale(row, from_=from_, to=to_, orient="horizontal",
                      variable=variable).pack(
                side="left", fill="x", expand=True)
            val_label = ttk.Label(row, width=7, anchor="e")
            val_label.pack(side="left", padx=(8, 4))
            def update(*_):
                try:
                    val_label.config(text=format(variable.get(), fmt))
                except Exception:
                    val_label.config(text="-")
            variable.trace_add("write", update)
            update()

        add_slider_row("Rotation (° dans JPEG)",  0,    359,  self.var_rotation,   ".0f")
        add_slider_row("Luminosité",              -1.0,  1.0,  self.var_brightness, ".2f")
        add_slider_row("Contraste",                0.0, 32.0,  self.var_contrast,   ".1f")
        add_slider_row("Saturation",               0.0, 32.0,  self.var_saturation, ".1f")
        add_slider_row("Qualité JPEG",             1,   100,   self.var_quality,    ".0f")

        if include_lens:
            add_slider_row(
                "Mise au point\n(0=∞  2=50cm  10=10cm)",
                IMX708_LENS_MIN, IMX708_LENS_MAX, self.var_lens, ".1f")
            ttk.Button(parent, text="Mise au point → Infini (∞)",
                       command=lambda: self.var_lens.set(
                           IMX708_LENS_INFINITY)).pack(pady=(2, 4))

        if include_hdr:
            row_hdr = ttk.Frame(parent)
            row_hdr.pack(fill="x", pady=3)
            ttk.Checkbutton(row_hdr, text="Mode HDR (IMX708)",
                            variable=self.var_hdr).pack(
                side="left", padx=4)

    def _build_exposition_awb(self, parent):
        """Panneau exposition manuelle + balance des blancs."""
        lf = ttk.LabelFrame(parent,
                             text="Exposition & Balance des blancs")
        lf.pack(fill="x", pady=(4, 2), padx=6)

        # -- Obturateur --
        row_sh = ttk.Frame(lf)
        row_sh.pack(fill="x", padx=6, pady=3)
        ttk.Checkbutton(row_sh, text="Exposition auto",
                        variable=self.var_shutter_auto).pack(
            side="left")
        ttk.Label(row_sh, text="  Shutter (µs) :").pack(side="left")
        e_sh = ttk.Entry(row_sh, textvariable=self.var_shutter_us,
                         width=8)
        e_sh.pack(side="left", padx=4)
        ttk.Label(row_sh, text="  Gain :").pack(side="left")
        ttk.Checkbutton(row_sh, text="Auto",
                        variable=self.var_gain_auto).pack(side="left")
        ttk.Entry(row_sh, textvariable=self.var_gain, width=6).pack(
            side="left", padx=4)

        # -- AWB --
        row_awb = ttk.Frame(lf)
        row_awb.pack(fill="x", padx=6, pady=3)
        ttk.Label(row_awb, text="Balance blancs :").pack(side="left")
        for mode, label in [("auto", "Auto"), ("incandescent", "Incand."),
                             ("fluorescent", "Fluores."),
                             ("daylight", "Jour"), ("cloudy", "Nuageux"),
                             ("manual", "Manuel")]:
            ttk.Radiobutton(row_awb, text=label,
                            variable=self.var_awb_mode,
                            value=mode).pack(side="left", padx=3)

        row_gains = ttk.Frame(lf)
        row_gains.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(row_gains,
                  text="Gains manuels – Rouge :").pack(side="left")
        ttk.Entry(row_gains, textvariable=self.var_awb_r,
                  width=5).pack(side="left", padx=3)
        ttk.Label(row_gains, text="  Bleu :").pack(side="left")
        ttk.Entry(row_gains, textvariable=self.var_awb_b,
                  width=5).pack(side="left", padx=3)
        ttk.Label(row_gains,
                  text="(actifs si mode Manuel)",
                  foreground="#555555",
                  font=("Arial", 8, "italic")).pack(side="left",
                                                    padx=6)

    def _build_rafale(self, parent):
        """Panneau mode rafale."""
        lf = ttk.LabelFrame(parent, text="Mode Rafale")
        lf.pack(fill="x", pady=(4, 2), padx=6)
        row = ttk.Frame(lf)
        row.pack(fill="x", padx=6, pady=6)
        ttk.Label(row, text="Nombre de photos :").pack(side="left")
        ttk.Entry(row, textvariable=self.var_burst_n,
                  width=5).pack(side="left", padx=4)
        ttk.Label(row, text="  Délai entre photos (s) :").pack(
            side="left")
        ttk.Entry(row, textvariable=self.var_burst_delay,
                  width=5).pack(side="left", padx=4)
        return lf

    # ----------------------------------------------------------
    # Miniature dernière photo capturée
    # ----------------------------------------------------------
    def _update_thumbnail(self, fichier: str, camera: str):
        """Charge et affiche la miniature de la dernière photo."""
        try:
            img = Image.open(fichier)
            img.thumbnail((200, 150), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            if camera == "IMX219":
                self._last_photo_219 = photo
                if self._thumb_label_219:
                    self._thumb_label_219.config(image=photo,
                                                 text="")
            else:
                self._last_photo_708 = photo
                if self._thumb_label_708:
                    self._thumb_label_708.config(image=photo,
                                                 text="")
        except Exception:
            pass

    # ----------------------------------------------------------
    # Onglet IMX219
    # ----------------------------------------------------------
    def _build_tab_imx219(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=0)
        tab.rowconfigure(0, weight=1)

        canvas = tk.Canvas(tab, borderwidth=0, highlightthickness=0)
        sb     = ttk.Scrollbar(tab, orient="vertical",
                               command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=2, sticky="ns")
        canvas.grid(row=0, column=0, sticky="nsew")

        frame_left = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=frame_left, anchor="nw")
        frame_left.bind("<Configure>",
                        lambda e: canvas.configure(
                            scrollregion=canvas.bbox("all")))

        frame_right = ttk.Frame(tab, width=200)
        frame_right.grid(row=0, column=1, sticky="ns", padx=(0, 6),
                         pady=4)
        frame_right.grid_propagate(False)

        ttk.Label(frame_left,
                  text="Caméra IMX219  –  8 Mpx – angle 120°",
                  font=("Arial", 12, "bold")).pack(pady=(8, 2))
        self.label_info_219 = ttk.Label(
            frame_left,
            text="Régler les paramètres & Choisir un mode photo.")
        self.label_info_219.pack(pady=4)

        frame_reg = ttk.LabelFrame(
            frame_left,
            text="Paramètres  (inclus rotation : fichier .jpg)")
        frame_reg.pack(pady=6, fill="x", padx=6)
        self._build_sliders(frame_reg)
        ttk.Button(frame_reg, text="Réinitialiser les réglages",
                   command=self._reset_reglages).pack(pady=(6, 4))

        self._build_exposition_awb(frame_left)

        frame_photo = ttk.LabelFrame(frame_left, text="Modes photo")
        frame_photo.pack(pady=6)
        ttk.Button(frame_photo,
                   text="Plan large  (3280 × 2464 · ~1,5 Mo)",
                   command=lambda: self._photo_imx219("large")).grid(
            row=0, column=0, padx=5, pady=5)
        ttk.Button(frame_photo,
                   text="Plan serré  (1920 × 1080 · < 450 Ko)",
                   command=lambda: self._photo_imx219("serre")).grid(
            row=0, column=1, padx=5, pady=5)

        # Rafale
        lf_rafale = self._build_rafale(frame_left)
        ttk.Button(lf_rafale, text="📸  Déclencher la rafale",
                   command=self._rafale_imx219).pack(pady=(2, 8))

        ttk.Separator(frame_left, orient="horizontal").pack(
            fill="x", pady=8)
        ttk.Label(frame_left, text="Vidéo",
                  font=("Arial", 11, "bold")).pack()
        ttk.Button(frame_left, text="Vidéo IMX219",
                   command=self._video_imx219).pack(pady=8)

        # Colonne droite : image caméra + miniature dernière photo
        ttk.Label(frame_right,
                  image=self.img_camera219).pack(side="top",
                                                 pady=(10, 4))
        ttk.Separator(frame_right, orient="horizontal").pack(
            fill="x", padx=4, pady=4)
        ttk.Label(frame_right, text="Dernière photo :",
                  font=("Arial", 9, "italic")).pack()
        self._thumb_label_219 = ttk.Label(
            frame_right,
            text="(aucune capture)",
            foreground="#888888",
            font=("Arial", 8, "italic"))
        self._thumb_label_219.pack(pady=4)
        ttk.Label(frame_right,
                  image=self.img_ventilo).pack(side="bottom",
                                               pady=(0, 10))

    # ----------------------------------------------------------
    # Onglet IMX708 Wide
    # ----------------------------------------------------------
    def _build_tab_imx708(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=0)
        tab.rowconfigure(0, weight=1)

        canvas = tk.Canvas(tab, borderwidth=0, highlightthickness=0)
        sb     = ttk.Scrollbar(tab, orient="vertical",
                               command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.grid(row=0, column=2, sticky="ns")
        canvas.grid(row=0, column=0, sticky="nsew")

        frame_left = ttk.Frame(canvas)
        canvas.create_window((0, 0), window=frame_left, anchor="nw")
        frame_left.bind("<Configure>",
                        lambda e: canvas.configure(
                            scrollregion=canvas.bbox("all")))

        frame_right = ttk.Frame(tab, width=200)
        frame_right.grid(row=0, column=1, sticky="ns", padx=(0, 6),
                         pady=4)
        frame_right.grid_propagate(False)

        ttk.Label(frame_left,
                  text="Caméra IMX708 – angle 120° & Autofocus",
                  font=("Arial", 12, "bold")).pack(pady=(8, 2))
        self.label_info_708 = ttk.Label(
            frame_left,
            text="Régler les paramètres & Choisir un mode photo.")
        self.label_info_708.pack(pady=4)

        frame_reg = ttk.LabelFrame(frame_left,
                                   text="Paramètres de prise de vue")
        frame_reg.pack(pady=6, fill="x", padx=6)
        self._build_sliders(frame_reg, include_lens=True,
                            include_hdr=True)
        ttk.Button(frame_reg,
                   text="Réinitialiser tous les réglages",
                   command=self._reset_reglages).pack(pady=(6, 4))

        self._build_exposition_awb(frame_left)

        frame_photo = ttk.LabelFrame(frame_left, text="Modes photo")
        frame_photo.pack(pady=6)
        ttk.Button(frame_photo,
                   text="Pleine résolution  (4608 × 2592 · ~2 Mo · 120°)",
                   command=lambda: self._photo_imx708("large")).grid(
            row=0, column=0, padx=5, pady=5)
        ttk.Button(frame_photo,
                   text="Résolution HD  (2304 × 1296 · < 1 Mo · 120°)",
                   command=lambda: self._photo_imx708("hd")).grid(
            row=0, column=1, padx=5, pady=5)

        lf_rafale = self._build_rafale(frame_left)
        ttk.Button(lf_rafale, text="📸  Déclencher la rafale",
                   command=self._rafale_imx708).pack(pady=(2, 8))

        ttk.Separator(frame_left, orient="horizontal").pack(
            fill="x", pady=8)
        ttk.Label(frame_left, text="Vidéo",
                  font=("Arial", 11, "bold")).pack()
        ttk.Button(frame_left, text="Vidéo IMX708 Wide",
                   command=self._video_imx708).pack(pady=8)

        ttk.Label(frame_right,
                  image=self.img_camera708).pack(side="top",
                                                 pady=(10, 4))
        ttk.Separator(frame_right, orient="horizontal").pack(
            fill="x", padx=4, pady=4)
        ttk.Label(frame_right, text="Dernière photo :",
                  font=("Arial", 9, "italic")).pack()
        self._thumb_label_708 = ttk.Label(
            frame_right,
            text="(aucune capture)",
            foreground="#888888",
            font=("Arial", 8, "italic"))
        self._thumb_label_708.pack(pady=4)
        ttk.Label(frame_right,
                  image=self.img_ventilo).pack(side="bottom",
                                               pady=(0, 10))

    # ----------------------------------------------------------
    # Onglet IMX500 / IA
    # ----------------------------------------------------------
    def _build_tab_imx500(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=0)
        tab.rowconfigure(0, weight=1)

        frame_left  = ttk.Frame(tab)
        frame_left.grid(row=0, column=0, sticky="nsew", padx=(6, 4),
                        pady=4)
        frame_right = ttk.Frame(tab, width=220)
        frame_right.grid(row=0, column=1, sticky="ns", padx=(0, 6),
                         pady=4)
        frame_right.grid_propagate(False)

        ttk.Label(frame_left,
                  text="Caméra IMX500  –  IA embarquée  (port : 1)",
                  font=("Arial", 12, "bold")).pack(pady=(8, 6))

        # ── Photo ──
        lf_photo = ttk.LabelFrame(frame_left, text="📷  Photographie")
        lf_photo.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(lf_photo,
                  text="Résolution maximale : 4056 × 3040  (~3 Mo)",
                  foreground="#444444",
                  font=("Arial", 9, "italic")).pack(
            anchor="w", padx=10, pady=(4, 2))
        ttk.Button(lf_photo,
                   text="📷  Prendre une photo  (4056 × 3040)",
                   command=self._photo_imx500,
                   width=40).pack(padx=10, pady=(2, 8))

        # ── Vidéo ──
        lf_video = ttk.LabelFrame(frame_left, text="🎬  Vidéo")
        lf_video.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(lf_video,
                  text="Fréquence maximale : 30 fps  (pas de HFR sur IMX500)",
                  foreground="#444444",
                  font=("Arial", 9, "italic")).pack(
            anchor="w", padx=10, pady=(4, 2))
        btn_row_vid = ttk.Frame(lf_video)
        btn_row_vid.pack(padx=10, pady=(2, 8))
        ttk.Button(btn_row_vid, text="🎬  Vidéo standard",
                   command=self._video_imx500_simple,
                   width=22).pack(side="left", padx=(0, 8))
        ttk.Button(btn_row_vid, text="🎬  Vidéo IA (recording)",
                   command=self._video_imx500_ia,
                   width=22).pack(side="left")

        # ── Seuil de confiance ──
        lf_conf = ttk.LabelFrame(frame_left,
                                 text="⚙️  Seuil de confiance (détection objets)")
        lf_conf.pack(fill="x", padx=6, pady=(0, 6))
        row_conf = ttk.Frame(lf_conf)
        row_conf.pack(fill="x", padx=10, pady=6)
        ttk.Label(row_conf, text="Seuil (0.0 – 1.0) :").pack(
            side="left")
        ttk.Scale(row_conf, from_=0.0, to=1.0, orient="horizontal",
                  variable=self.var_ia_confidence,
                  length=200).pack(side="left", padx=6)
        lbl_conf = ttk.Label(row_conf, width=5)
        lbl_conf.pack(side="left")
        def _upd_conf(*_):
            lbl_conf.config(
                text=f"{self.var_ia_confidence.get():.2f}")
        self.var_ia_confidence.trace_add("write", _upd_conf)
        _upd_conf()
        ttk.Label(lf_conf,
                  text="(0.5 = défaut IMX500. Abaissez pour plus "
                       "de détections, augmentez pour moins de faux positifs.)",
                  foreground="#555555",
                  font=("Arial", 8, "italic"),
                  wraplength=480,
                  justify="left").pack(anchor="w", padx=10,
                                      pady=(0, 6))

        # ── Log détections ──
        lf_log = ttk.LabelFrame(frame_left,
                                text="📋  Export log des détections IA")
        lf_log.pack(fill="x", padx=6, pady=(0, 6))
        row_log1 = ttk.Frame(lf_log)
        row_log1.pack(fill="x", padx=10, pady=(6, 2))
        ttk.Checkbutton(row_log1, text="Activer l'enregistrement",
                        variable=self.var_ia_log_active,
                        command=self._ia_log_toggle).pack(side="left")
        ttk.Label(row_log1, text="  Format :").pack(side="left")
        ttk.Radiobutton(row_log1, text="CSV",
                        variable=self.var_ia_log_fmt,
                        value="csv").pack(side="left", padx=4)
        ttk.Radiobutton(row_log1, text="JSON",
                        variable=self.var_ia_log_fmt,
                        value="json").pack(side="left", padx=4)
        row_log2 = ttk.Frame(lf_log)
        row_log2.pack(fill="x", padx=10, pady=(2, 8))
        ttk.Label(row_log2, text="Fichier :").pack(side="left")
        ttk.Entry(row_log2, textvariable=self.var_ia_log_path,
                  width=32).pack(side="left", padx=4,
                                 fill="x", expand=True)
        ttk.Button(row_log2, text="…", width=3,
                   command=self._ia_log_choose_path).pack(side="left")

        # ── Modes IA continus ──
        lf_ia = ttk.LabelFrame(
            frame_left,
            text="🎬  Modes IA continus  "
                 "(arrêt via « Arrêter mode continu »)")
        lf_ia.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Label(lf_ia,
                  text="Traitement IA embarqué dans le capteur IMX500 "
                       "– résultats en temps réel",
                  foreground="#444444",
                  font=("Arial", 9, "italic")).pack(
            anchor="w", padx=10, pady=(4, 2))

        ia_grid = ttk.Frame(lf_ia)
        ia_grid.pack(padx=10, pady=(4, 8))
        # True = compatible log CSV, False = non compatible
        ia_buttons = [
            ("🔍  Détection d'objets",
             lambda: self._lancer_detection_objets(), 0, 0, False),
            ("🎬  PoseNet",
             lambda: self.lancer_continu(self.ia_ctrl.ia_pose,
                                          delay_ms=20000), 0, 1, False),
            ("🎬  HigherHRNet",
             lambda: self.lancer_continu(self.ia_ctrl.ia_higherhrnet,
                                          delay_ms=30000),
             1, 0, True),
            ("🏷️  Segmentation",
             lambda: self.lancer_continu(self.ia_ctrl.ia_segmentation,
                                          delay_ms=30000),
             1, 1, True),
            ("🏷️  Classification",
             lambda: self.lancer_continu(self.ia_ctrl.ia_classification,
                                          delay_ms=30000),
             2, 0, True),
            ("⚡  Multi-Processing",
             lambda: self.lancer_continu(
                 self.ia_ctrl.ia_detection_rapide, delay_ms=30000),
             2, 1, True),
        ]
        self._ia_btns = []  # (bouton, log_compatible)
        for label, func, r, c, log_ok in ia_buttons:
            btn = tk.Button(ia_grid, text=label, width=26, command=func,
                            relief="raised", bd=2)
            btn.grid(row=r, column=c, padx=6, pady=4, sticky="ew")
            # Capturer la couleur neutre réelle du système
            btn._neutral_bg = btn.cget("bg")
            btn._neutral_fg = btn.cget("fg")
            self._ia_btns.append((btn, log_ok))
        ia_grid.columnconfigure(0, weight=1)
        ia_grid.columnconfigure(1, weight=1)

        ttk.Label(frame_right,
                  image=self.img_imx500).pack(side="top",
                                              pady=(10, 0))
        ttk.Label(frame_right,
                  image=self.img_ventilo).pack(side="bottom",
                                               pady=(0, 10))

    # ----------------------------------------------------------
    # Onglet Motion
    # ----------------------------------------------------------
    def _build_tab_motion(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(0, weight=1)
        frame = ttk.Frame(tab)
        frame.pack(expand=True, fill="both", padx=10, pady=10)
        self.btn_web    = None
        self.btn_stream = None

        ttk.Label(frame,
                  text="Détection de mouvement – Motion\n"
                       "Compatible IMX219 et IMX708",
                  font=("Arial", 12, "bold"),
                  justify="center").pack(pady=(8, 2))

        self.label_motion_cam = ttk.Label(frame,
                                          font=("Arial", 9, "italic"),
                                          foreground="#555555")
        self.label_motion_cam.pack(pady=(0, 6))
        self._update_motion_cam_label()
        self.var_cam0.trace_add("write",
                                lambda *_: self._update_motion_cam_label())

        self.label_motion_status = ttk.Label(frame, text="…",
                                             font=("Arial", 10))
        self.label_motion_status.pack(pady=4)

        frame_diag = ttk.LabelFrame(frame,
                                    text="Diagnostics RPi5 / Bookworm")
        frame_diag.pack(pady=6, fill="x", padx=20)

        lc_ok    = MotionController.check_libcamerify()
        patch_ok = MotionController.check_service_patched()

        ttk.Label(frame_diag,
                  text=f"{'✅' if lc_ok else '❌'}  "
                       f"libcamerify présent ({LIBCAMERIFY})",
                  foreground="black").pack(anchor="w", padx=10, pady=2)
        ttk.Label(frame_diag,
                  text=f"{'✅' if patch_ok else '❌'}  "
                       f"motion.service utilise libcamerify"
                       + ("" if patch_ok else "  ← à corriger !"),
                  foreground="black" if patch_ok else "red").pack(
            anchor="w", padx=10, pady=2)

        if not patch_ok:
            ttk.Label(frame_diag,
                      text="   Corriger avec : sudo nano "
                           "/usr/lib/systemd/system/motion.service\n"
                           "   Remplacer ExecStart=/usr/bin/motion\n"
                           "   par ExecStart=/usr/bin/libcamerify "
                           "/usr/bin/motion\n"
                           "   puis : sudo systemctl daemon-reload",
                      foreground="red",
                      justify="left").pack(anchor="w", padx=10,
                                           pady=(0, 6))

        ttk.Label(frame_diag,
                  text=f"ℹ  Timeout systemctl : "
                       f"{MOTION_SYSTEMCTL_TIMEOUT} s  –  "
                       f"Délai arrêt : {MOTION_STOP_GRACE} s",
                  foreground="#555555",
                  font=("Arial", 8, "italic")).pack(
            anchor="w", padx=10, pady=(0, 6))

        frame_btn = ttk.LabelFrame(frame,
                                   text="Contrôle du service Motion")
        frame_btn.pack(pady=6, fill="x", padx=20)

        # ── Sélecteur de mode (toggle) ──────────────────────────
        mode_lf = ttk.LabelFrame(frame_btn, text="Mode Motion")
        mode_lf.pack(fill="x", padx=10, pady=(8, 4))

        # Lire le mode actuel depuis motion.conf
        movie_on = MotionController.get_movie_output()
        self.motion_mode = tk.StringVar(
            value="enregistrement" if movie_on else "flux")

        ttk.Label(mode_lf,
                  text="Choisir le mode avant de démarrer/redémarrer :",
                  foreground="#555555",
                  font=("Arial", 9, "italic")).pack(anchor="w",
                                                    padx=8, pady=(4, 2))
        mode_row = ttk.Frame(mode_lf)
        mode_row.pack(pady=(2, 6))
        self.btn_mode_enreg = tk.Button(
            mode_row,
            text="🎥  Enregistrement .mkv\n(détection de mouvement)",
            width=26, height=2,
            relief="sunken" if movie_on else "raised",
            command=self._motion_mode_enregistrement,
        )
        self.btn_mode_enreg.pack(side="left", padx=8)
        self.btn_mode_flux = tk.Button(
            mode_row,
            text="📡  Flux continu\n(stream HTTP 8081, pour Visio)",
            width=26, height=2,
            relief="raised" if movie_on else "sunken",
            command=self._motion_mode_flux,
        )
        self.btn_mode_flux.pack(side="left", padx=8)

        self.label_mode_actif = ttk.Label(
            mode_lf,
            font=("Arial", 9, "bold"),
            foreground="#005588")
        self.label_mode_actif.pack(pady=(0, 6))
        self._update_mode_label()

        # ── Boutons de contrôle ─────────────────────────────────
        btn_row = ttk.Frame(frame_btn)
        btn_row.pack(pady=8)
        ttk.Button(btn_row, text="▶  Démarrer", width=16,
                   command=self._motion_start).pack(side="left", padx=5)
        ttk.Button(btn_row, text="■  Arrêter", width=16,
                   command=self._motion_stop).pack(side="left", padx=5)
        ttk.Button(btn_row, text="↺  Redémarrer", width=16,
                   command=self._motion_restart).pack(side="left",
                                                      padx=5)

        btn_row2 = ttk.Frame(frame_btn)
        btn_row2.pack(pady=(0, 8))
        style = ttk.Style()
        style.configure("Success.TButton", foreground="black",
                        background="#00cc44")
        style.map("Success.TButton",
                  background=[("active", "#00ff55")])
        style.configure("Danger.TButton", foreground="white",
                        background="#cc0000")
        style.map("Danger.TButton",
                  background=[("active", "#ff0000")])
        style.configure("Repair.TButton", foreground="white",
                        background="#cc6600")
        style.map("Repair.TButton",
                  background=[("active", "#ff8800")])

        ttk.Button(btn_row2, text="Rafraîchir le statut", width=20,
                   command=self._update_motion_status,
                   style="Success.TButton").pack(side="left", padx=5)
        ttk.Button(btn_row2, text="RaZ Motion Captures", width=20,
                   command=self._raz_motion_captures,
                   style="Danger.TButton").pack(side="left", padx=5)

        btn_row3 = ttk.Frame(frame_btn)
        btn_row3.pack(pady=(0, 8))
        self.btn_web = ttk.Button(btn_row3,
                                  text="Interface Web (8080)", width=20,
                                  command=self._open_web_if_running)
        self.btn_web.pack(side="left", padx=5)
        self.btn_stream = ttk.Button(btn_row3,
                                     text="Stream Live (8081)", width=20,
                                     command=self._open_stream_if_running)
        self.btn_stream.pack(side="left", padx=5)

        frame_repair = ttk.LabelFrame(frame,
                                      text="Réparation des vidéos Motion")
        frame_repair.pack(pady=6, fill="x", padx=20)
        ttk.Label(frame_repair,
                  text="Si Motion a été arrêté brutalement, les fichiers "
                       "MP4/MKV peuvent être illisibles.\n"
                       "Cliquez sur « Réparer » pour les corriger via ffmpeg.",
                  foreground="#555555",
                  font=("Arial", 9, "italic"),
                  justify="left").pack(anchor="w", padx=10,
                                      pady=(4, 2))
        repair_row = ttk.Frame(frame_repair)
        repair_row.pack(padx=10, pady=(2, 8))
        ttk.Button(repair_row,
                   text="Réparer les vidéos motion_captures",
                   width=36,
                   command=self._motion_reparer_videos,
                   style="Repair.TButton").pack(side="left",
                                                padx=(0, 10))
        self.motion_repair_log = ttk.Label(frame_repair, text="",
                                           foreground="#333333",
                                           font=("Arial", 9),
                                           justify="left")
        self.motion_repair_log.pack(anchor="w", padx=10, pady=(0, 6))
        self._update_motion_status()

    # ----------------------------------------------------------
    # Onglet Timelapse
    # ----------------------------------------------------------
    def _build_tab_timelapse(self, tab):
        tab.columnconfigure(0, weight=0, minsize=340)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(0, weight=0)
        tab.rowconfigure(1, weight=1)

        ttk.Label(tab,
                  text="Timelapse – IMX708 Wide 120° / IMX500",
                  font=("Arial", 12, "bold")).grid(
            row=0, column=0, columnspan=2, pady=(10, 4))

        left = ttk.LabelFrame(tab, text="Paramètres")
        left.grid(row=1, column=0, sticky="nsew", padx=(10, 4), pady=6)
        left.columnconfigure(1, weight=1)

        ttk.Label(left, text="Caméra :").grid(
            row=0, column=0, sticky="w", padx=6, pady=4)
        self.tl_var_camera = tk.StringVar(value="IMX708")
        cam_frame = ttk.Frame(left)
        cam_frame.grid(row=0, column=1, sticky="w", padx=4, pady=4)
        ttk.Radiobutton(cam_frame, text="IMX708  (port 0)",
                        variable=self.tl_var_camera,
                        value="IMX708").pack(side="left", padx=(0, 8))
        ttk.Radiobutton(cam_frame, text="IMX500  (port 1)",
                        variable=self.tl_var_camera,
                        value="IMX500").pack(side="left")

        ttk.Label(left, text="Intervalle (s) :").grid(
            row=1, column=0, sticky="w", padx=6, pady=4)
        self.tl_interval = tk.IntVar(value=30)
        ttk.Entry(left, textvariable=self.tl_interval,
                  width=8).grid(row=1, column=1, sticky="w", padx=4)

        ttk.Label(left, text="Durée totale (min) :").grid(
            row=2, column=0, sticky="w", padx=6, pady=4)
        self.tl_duration = tk.IntVar(value=10)
        ttk.Entry(left, textvariable=self.tl_duration,
                  width=8).grid(row=2, column=1, sticky="w", padx=4)

        ttk.Label(left, text="Largeur (px) :").grid(
            row=3, column=0, sticky="w", padx=6, pady=4)
        self.tl_width = tk.IntVar(value=2304)
        ttk.Entry(left, textvariable=self.tl_width,
                  width=8).grid(row=3, column=1, sticky="w", padx=4)

        ttk.Label(left, text="Hauteur (px) :").grid(
            row=4, column=0, sticky="w", padx=6, pady=4)
        self.tl_height = tk.IntVar(value=1296)
        ttk.Entry(left, textvariable=self.tl_height,
                  width=8).grid(row=4, column=1, sticky="w", padx=4)

        self.tl_hdr = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text="Mode HDR (IMX708 seulement)",
                        variable=self.tl_hdr).grid(
            row=5, column=0, columnspan=2, sticky="w", padx=6, pady=4)

        # AWB lock
        self.tl_awb_lock = tk.BooleanVar(value=False)
        ttk.Checkbutton(left,
                        text="AWB lock (anti-scintillement)",
                        variable=self.tl_awb_lock,
                        command=self._tl_awb_lock_changed).grid(
            row=6, column=0, columnspan=2, sticky="w", padx=6, pady=2)

        row_awbg = ttk.Frame(left)
        row_awbg.grid(row=7, column=0, columnspan=2, sticky="w",
                      padx=20, pady=2)
        ttk.Label(row_awbg, text="Gains (rouge / bleu) :").pack(
            side="left")
        self.tl_awb_r = tk.DoubleVar(value=2.0)
        self.tl_awb_b = tk.DoubleVar(value=1.8)
        self.tl_entry_r = ttk.Entry(row_awbg, textvariable=self.tl_awb_r,
                                    width=5, state="disabled")
        self.tl_entry_r.pack(side="left", padx=3)
        ttk.Label(row_awbg, text=" / ").pack(side="left")
        self.tl_entry_b = ttk.Entry(row_awbg, textvariable=self.tl_awb_b,
                                    width=5, state="disabled")
        self.tl_entry_b.pack(side="left", padx=3)
        ttk.Label(row_awbg,
                  text="(laisser vide = AWB off sans gains fixes)",
                  foreground="#555555",
                  font=("Arial", 8, "italic")).pack(side="left", padx=4)

        ttk.Label(left, text="Dossier photos :").grid(
            row=8, column=0, sticky="w", padx=6, pady=4)
        self.tl_output_dir = tk.StringVar(
            value=os.path.join(DEFAULT_SAVE_DIR, "Timelapse_frames"))
        dir_frame = ttk.Frame(left)
        dir_frame.grid(row=8, column=1, sticky="ew", padx=4)
        ttk.Entry(dir_frame, textvariable=self.tl_output_dir,
                  width=18).pack(side="left", fill="x", expand=True)
        ttk.Button(dir_frame, text="…", width=3,
                   command=self._tl_choose_dir).pack(side="left",
                                                     padx=2)

        ttk.Label(left, text="FPS vidéo MP4 :").grid(
            row=9, column=0, sticky="w", padx=6, pady=4)
        self.tl_fps = tk.IntVar(value=24)
        ttk.Entry(left, textvariable=self.tl_fps,
                  width=8).grid(row=9, column=1, sticky="w", padx=4)

        # Infos simples
        self.tl_label_info = ttk.Label(left, text="",
                                       foreground="#555555",
                                       font=("Arial", 9, "italic"))
        self.tl_label_info.grid(row=10, column=0, columnspan=2,
                                pady=(2, 2), padx=6, sticky="w")
        for v in (self.tl_interval, self.tl_duration, self.tl_fps,
                  self.tl_width, self.tl_height):
            v.trace_add("write", lambda *_: self._tl_update_info())
        self._tl_update_info()

        # ── Estimations disque ──
        ttk.Separator(left, orient="horizontal").grid(
            row=11, column=0, columnspan=2, sticky="ew", padx=6,
            pady=4)
        lf_est = ttk.LabelFrame(left, text="📊  Estimations espace disque")
        lf_est.grid(row=12, column=0, columnspan=2, sticky="ew",
                    padx=6, pady=(0, 4))
        self.tl_lbl_est_frames = ttk.Label(
            lf_est, text="", foreground="#333333",
            font=("Arial", 9))
        self.tl_lbl_est_frames.pack(anchor="w", padx=8, pady=(4, 1))
        self.tl_lbl_est_video = ttk.Label(
            lf_est, text="", foreground="#333333",
            font=("Arial", 9))
        self.tl_lbl_est_video.pack(anchor="w", padx=8, pady=1)
        self.tl_lbl_est_total = ttk.Label(
            lf_est, text="", foreground="#005588",
            font=("Arial", 9, "bold"))
        self.tl_lbl_est_total.pack(anchor="w", padx=8, pady=1)
        self.tl_lbl_est_disk = ttk.Label(
            lf_est, text="", foreground="#333333",
            font=("Arial", 9))
        self.tl_lbl_est_disk.pack(anchor="w", padx=8, pady=(1, 6))
        ttk.Label(lf_est,
                  text="⚠ Valeurs indicatives – la taille JPEG réelle "
                       "dépend du contenu de la scène.",
                  foreground="#888888",
                  font=("Arial", 8, "italic")).pack(
            anchor="w", padx=8, pady=(0, 4))

        # ── Prévisualisation ──
        ttk.Separator(left, orient="horizontal").grid(
            row=13, column=0, columnspan=2, sticky="ew", padx=6,
            pady=4)
        preview_lf = ttk.LabelFrame(left,
                                    text="Prévisualisation caméra")
        preview_lf.grid(row=14, column=0, columnspan=2, sticky="ew",
                        padx=6, pady=(0, 8))
        ttk.Label(preview_lf,
                  text="Ouvre une fenêtre Live pour cadrer la scène.",
                  foreground="#555555",
                  font=("Arial", 9, "italic")).pack(
            anchor="w", padx=6, pady=(4, 2))
        prev_btn_row = ttk.Frame(preview_lf)
        prev_btn_row.pack(pady=(2, 6))
        ttk.Button(prev_btn_row, text="▶  Lancer prévisualisation",
                   command=self._tl_preview_start).pack(
            side="left", padx=5)
        ttk.Button(prev_btn_row, text="■  Arrêter prévisualisation",
                   command=self._tl_preview_stop).pack(
            side="left", padx=5)

        # ── Reprise de session ──
        ttk.Separator(left, orient="horizontal").grid(
            row=15, column=0, columnspan=2, sticky="ew", padx=6,
            pady=2)
        resume_lf = ttk.LabelFrame(left, text="↩  Reprise de session")
        resume_lf.grid(row=16, column=0, columnspan=2, sticky="ew",
                       padx=6, pady=(0, 8))
        self.tl_lbl_resume = ttk.Label(
            resume_lf, text="Aucune session interrompue détectée.",
            foreground="#555555", font=("Arial", 9, "italic"))
        self.tl_lbl_resume.pack(anchor="w", padx=8, pady=(4, 2))
        self.tl_btn_resume = ttk.Button(
            resume_lf, text="↩  Reprendre la session précédente",
            command=self._tl_resume, state="disabled")
        self.tl_btn_resume.pack(padx=8, pady=(2, 6))

        # ── Colonne droite : contrôles + journal ──
        mid = ttk.Frame(tab)
        mid.grid(row=1, column=1, sticky="nsew", padx=(4, 4), pady=6)
        mid.rowconfigure(1, weight=1)

        ctrl = ttk.LabelFrame(mid, text="Lancement")
        ctrl.pack(fill="x", pady=(0, 6))
        btn_row = ttk.Frame(ctrl)
        btn_row.pack(pady=8)
        self.tl_btn_start = ttk.Button(btn_row, text="▶  Démarrer",
                                       width=16,
                                       command=self._tl_start)
        self.tl_btn_start.pack(side="left", padx=5)
        self.tl_btn_stop = ttk.Button(btn_row, text="■  Arrêter",
                                      width=16,
                                      command=self._tl_stop,
                                      state="disabled")
        self.tl_btn_stop.pack(side="left", padx=5)
        self.tl_label_status = ttk.Label(ctrl, text="En attente.",
                                         foreground="#555555",
                                         font=("Arial", 10, "bold"))
        self.tl_label_status.pack(pady=(0, 4))

        ttk.Separator(mid, orient="horizontal").pack(fill="x", pady=4)

        video_lf = ttk.LabelFrame(mid, text="Assembler en vidéo MP4")
        video_lf.pack(fill="x", pady=(0, 6))
        ttk.Label(video_lf,
                  text="Utilise toutes les photos tl_*.jpg du "
                       "dossier sélectionné.",
                  foreground="#555555",
                  font=("Arial", 9, "italic")).pack(
            anchor="w", padx=6, pady=(4, 2))
        video_row = ttk.Frame(video_lf)
        video_row.pack(fill="x", padx=6, pady=(2, 8))
        ttk.Button(video_row, text="🎬  Créer la vidéo MP4",
                   command=self._tl_create_video).pack(
            side="left", padx=(0, 12))
        if self.img_timelapse:
            ttk.Label(video_row, image=self.img_timelapse,
                      text="Timelapse", compound="top",
                      font=("Arial", 9, "italic"),
                      foreground="#555555").pack(side="right")

        log_frame = ttk.LabelFrame(mid, text="Journal")
        log_frame.pack(fill="both", expand=True)
        self.tl_log = tk.Text(log_frame, height=10, state="disabled",
                              wrap="word")
        sb = ttk.Scrollbar(log_frame, command=self.tl_log.yview)
        self.tl_log.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tl_log.pack(fill="both", expand=True)

    # ----------------------------------------------------------
    # Onglet Slow Motion
    # ----------------------------------------------------------
    def _build_tab_slowmo(self, tab):
        tab.columnconfigure(0, weight=0, minsize=340)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(1, weight=1)

        ttk.Label(tab,
                  text="Slow Motion – Capture haute fréquence & "
                       "Conversion ralenti",
                  font=("Arial", 12, "bold")).grid(
            row=0, column=0, columnspan=2, pady=(10, 4))

        left = ttk.Frame(tab)
        left.grid(row=1, column=0, sticky="nsew", padx=(10, 4), pady=4)

        lf_hfr = ttk.LabelFrame(
            left, text="⚙️  Capture Haute Fréquence  (IMX219 / IMX708)")
        lf_hfr.pack(fill="x", pady=(0, 8))
        ttk.Label(lf_hfr,
                  text="IMX500 limité à 30 fps – non disponible.",
                  foreground="#aa6600",
                  font=("Arial", 9, "italic")).pack(
            anchor="w", padx=8, pady=(4, 2))

        ttk.Label(lf_hfr, text="Mode de capture :").pack(
            anchor="w", padx=8, pady=(2, 0))
        self.sm_var_mode = tk.IntVar(value=0)
        for i, (label, *_) in enumerate(SM_CAPTURE_MODES):
            ttk.Radiobutton(lf_hfr, text=label,
                            variable=self.sm_var_mode,
                            value=i).pack(anchor="w", padx=20, pady=1)

        dur_row = ttk.Frame(lf_hfr)
        dur_row.pack(anchor="w", padx=8, pady=(6, 2))
        ttk.Label(dur_row, text="Durée de capture (s) :").pack(
            side="left")
        self.sm_var_duree = tk.IntVar(value=5)
        ttk.Entry(dur_row, textvariable=self.sm_var_duree,
                  width=6).pack(side="left", padx=6)

        self.sm_label_facteur = ttk.Label(lf_hfr, foreground="#005588",
                                          font=("Arial", 9, "italic"))
        self.sm_label_facteur.pack(anchor="w", padx=8, pady=(2, 4))
        self.sm_var_mode.trace_add("write",
                                   lambda *_: self._sm_update_facteur())
        self._sm_update_facteur()

        btn_hfr_row = ttk.Frame(lf_hfr)
        btn_hfr_row.pack(padx=8, pady=(2, 8))
        ttk.Button(btn_hfr_row, text="Capturer en HFR", width=22,
                   command=self._sm_capturer_hfr).pack(
            side="left", padx=(0, 8))
        ttk.Button(btn_hfr_row, text="Choisir dossier", width=18,
                   command=self._sm_choose_dir).pack(side="left")

        sm_dir_row = ttk.Frame(lf_hfr)
        sm_dir_row.pack(fill="x", padx=8, pady=(0, 6))
        ttk.Label(sm_dir_row, text="Dossier :").pack(side="left")
        self.sm_var_dir = tk.StringVar(value=self.camera_ctrl.save_dir)
        ttk.Label(sm_dir_row, textvariable=self.sm_var_dir,
                  foreground="#444444",
                  font=("Arial", 9)).pack(side="left", padx=6)

        lf_lire = ttk.LabelFrame(left,
                                 text="▶  Lecture ralenti dans VLC")
        lf_lire.pack(fill="x", pady=(0, 8))
        ttk.Label(lf_lire,
                  text="Ouvre n'importe quelle vidéo dans VLC ou "
                       "le lecteur système.",
                  foreground="#444444",
                  font=("Arial", 9, "italic")).pack(
            anchor="w", padx=8, pady=(4, 2))
        ttk.Label(lf_lire,
                  text="Compatible IMX219 / IMX708 / IMX500 (.MP4).",
                  foreground="#007700",
                  font=("Arial", 9, "italic")).pack(
            anchor="w", padx=8, pady=(0, 4))
        ttk.Button(lf_lire,
                   text="Choisir une Vidéo & Ouvrir avec VLC",
                   width=38,
                   command=self._sm_ouvrir_vlc).pack(
            padx=8, pady=(0, 8))

        if self.img_slowmo:
            ttk.Label(left,
                      image=self.img_slowmo).pack(side="bottom",
                                                  pady=(8, 4))

        right = ttk.Frame(tab)
        right.grid(row=1, column=1, sticky="nsew",
                   padx=(4, 10), pady=4)
        right.rowconfigure(1, weight=1)

        lf_conv = ttk.LabelFrame(
            right,
            text="⚙️  Conversion d'une vidéo existante en ralenti")
        lf_conv.pack(fill="x", pady=(0, 6))
        ttk.Label(lf_conv,
                  text="Compatible avec toutes les caméras "
                       "(IMX219, IMX708, IMX500).",
                  foreground="#007700",
                  font=("Arial", 9, "italic")).pack(
            anchor="w", padx=8, pady=(4, 2))

        src_row = ttk.Frame(lf_conv)
        src_row.pack(fill="x", padx=8, pady=(4, 2))
        ttk.Label(src_row, text="Source :", width=14).pack(side="left")
        self.sm_var_src = tk.StringVar(value="")
        ttk.Entry(src_row, textvariable=self.sm_var_src,
                  width=28).pack(side="left", padx=4,
                                 fill="x", expand=True)
        ttk.Button(src_row, text="…", width=3,
                   command=self._sm_choose_source).pack(side="left")

        fps_src_row = ttk.Frame(lf_conv)
        fps_src_row.pack(fill="x", padx=8, pady=(4, 2))
        ttk.Label(fps_src_row, text="FPS source :",
                  width=14).pack(side="left")
        self.sm_var_fps_src = tk.IntVar(value=120)
        ttk.Entry(fps_src_row, textvariable=self.sm_var_fps_src,
                  width=6).pack(side="left", padx=4)
        self.sm_label_fps_detect = ttk.Label(fps_src_row, text="",
                                             foreground="#005588",
                                             font=("Arial", 9,
                                                   "italic"))
        self.sm_label_fps_detect.pack(side="left", padx=8)
        ttk.Button(fps_src_row, text="🔍 Détecter", width=12,
                   command=self._sm_detecter_fps).pack(side="left")

        fps_tgt_row = ttk.Frame(lf_conv)
        fps_tgt_row.pack(fill="x", padx=8, pady=(4, 2))
        ttk.Label(fps_tgt_row, text="FPS sortie :",
                  width=14).pack(side="left")
        self.sm_var_fps_tgt = tk.IntVar(value=SM_OUTPUT_FPS)
        ttk.Entry(fps_tgt_row, textvariable=self.sm_var_fps_tgt,
                  width=6).pack(side="left", padx=4)
        ttk.Label(fps_tgt_row, text="(par défaut 30 fps)",
                  foreground="#555555",
                  font=("Arial", 9, "italic")).pack(
            side="left", padx=8)

        self.sm_label_conv_facteur = ttk.Label(lf_conv,
                                               foreground="#005588",
                                               font=("Arial", 9,
                                                     "italic"))
        self.sm_label_conv_facteur.pack(anchor="w", padx=8,
                                        pady=(2, 2))
        for v in (self.sm_var_fps_src, self.sm_var_fps_tgt):
            v.trace_add("write",
                        lambda *_: self._sm_update_conv_facteur())
        self._sm_update_conv_facteur()

        meth_row = ttk.Frame(lf_conv)
        meth_row.pack(fill="x", padx=8, pady=(6, 2))
        ttk.Label(meth_row, text="Méthodes :").pack(anchor="w")
        self.sm_var_methode = tk.StringVar(value="setpts")
        rb_frame = ttk.Frame(lf_conv)
        rb_frame.pack(fill="x", padx=16, pady=(0, 4))
        ttk.Radiobutton(rb_frame,
                        text="setpts  –  Rapide, simple, sans interpolation",
                        variable=self.sm_var_methode,
                        value="setpts").pack(anchor="w", pady=1)
        ttk.Label(rb_frame,
                  text="     Les images d'origine sont étirées dans "
                       "le temps. Rendu immédiat.",
                  foreground="#555555",
                  font=("Arial", 8, "italic")).pack(anchor="w")
        ttk.Radiobutton(rb_frame,
                        text="minterpolate  –  Interpolation (plus "
                             "fluide, très lent)",
                        variable=self.sm_var_methode,
                        value="minterpolate").pack(anchor="w",
                                                   pady=(6, 1))
        ttk.Label(rb_frame,
                  text="     ffmpeg génère des images intermédiaires."
                       " Peut prendre plusieurs minutes.",
                  foreground="#555555",
                  font=("Arial", 8, "italic")).pack(anchor="w")

        ttk.Button(lf_conv,
                   text="⚙️  Convertir en vidéo ralenti MP4",
                   width=36,
                   command=self._sm_convertir).pack(padx=8,
                                                    pady=(8, 10))

        log_lf = ttk.LabelFrame(right, text="Journal")
        log_lf.pack(fill="both", expand=True)
        self.sm_log = tk.Text(log_lf, height=8, state="disabled",
                              wrap="word")
        sm_sb = ttk.Scrollbar(log_lf, command=self.sm_log.yview)
        self.sm_log.config(yscrollcommand=sm_sb.set)
        sm_sb.pack(side="right", fill="y")
        self.sm_log.pack(fill="both", expand=True)

    # ----------------------------------------------------------
    # Onglet Fichiers
    # ----------------------------------------------------------
    def _build_tab_fichiers(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.rowconfigure(1, weight=1)

        top = ttk.Frame(tab)
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        ttk.Label(top, text="Explorateur – Dossier de sauvegarde",
                  font=("Arial", 12, "bold")).pack(side="left")
        ttk.Button(top, text="🔄  Actualiser",
                   command=self._fich_refresh).pack(side="right",
                                                    padx=5)
        ttk.Button(top, text="📂  Ouvrir dans le gestionnaire",
                   command=self._fich_open_manager).pack(
            side="right", padx=5)
        ttk.Button(top, text="🗑  Supprimer sélection",
                   command=self._fich_delete).pack(side="right",
                                                   padx=5)

        frame_list = ttk.Frame(tab)
        frame_list.grid(row=1, column=0, sticky="nsew",
                        padx=10, pady=(0, 6))
        frame_list.columnconfigure(0, weight=1)
        frame_list.rowconfigure(0, weight=1)

        cols = ("nom", "taille", "date")
        self.fich_tree = ttk.Treeview(frame_list, columns=cols,
                                      show="headings",
                                      selectmode="extended")
        self.fich_tree.heading("nom",    text="Nom")
        self.fich_tree.heading("taille", text="Taille")
        self.fich_tree.heading("date",   text="Date de modification")
        self.fich_tree.column("nom",    width=380, anchor="w")
        self.fich_tree.column("taille", width=90,  anchor="e")
        self.fich_tree.column("date",   width=160, anchor="center")

        vsb = ttk.Scrollbar(frame_list, orient="vertical",
                             command=self.fich_tree.yview)
        hsb = ttk.Scrollbar(frame_list, orient="horizontal",
                             command=self.fich_tree.xview)
        self.fich_tree.configure(yscrollcommand=vsb.set,
                                  xscrollcommand=hsb.set)
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        self.fich_tree.grid(row=0, column=0, sticky="nsew")

        self.fich_tree.bind("<Double-1>", self._fich_open_file)

        self._fich_refresh()

    # ===========================================================
    # Actions IMX219
    # ===========================================================
    def _photo_imx219(self, mode):
        if not self._check_cam0("IMX219"):
            return
        if not self._check_motion_conflict("IMX219"):
            return
        info = ("3280 × 2464 (~1,5 Mo)"
                if mode == "large" else "1920 × 1080 (< 450 Ko)")
        self.label_info_219.config(text=info)
        nom = self.askstring_kp("Fichier", "Nom (vide = défaut) :")
        if nom is None:
            self.label_info_219.config(text="")
            return
        nom = nom.strip() or None
        self.label_info_219.config(text="Capture en cours…")
        awb_mode, awb_gains = self._get_awb()
        def _capture():
            f = self.camera_ctrl.prendre_photo_imx219(
                resolution=mode, nom=nom,
                rotation=self._get_rotation(),
                brightness=self._get_brightness(),
                contrast=self._get_contrast(),
                saturation=self._get_saturation(),
                quality=self._get_quality(),
                shutter_us=self._get_shutter(),
                gain=self._get_gain(),
                awb_mode=awb_mode,
                awb_gains=awb_gains,
            )
            if f:
                self.root.after(0, lambda: self.label_info_219.config(
                    text="✅ Capture terminée."))
                self.root.after(0, lambda: self._update_thumbnail(f, "IMX219"))
            else:
                self.root.after(0, lambda: self.label_info_219.config(
                    text="❌ Échec capture – vérifiez la caméra.",
                    foreground="red"))
        threading.Thread(target=_capture, daemon=True).start()

    def _rafale_imx219(self):
        if not self._check_cam0("IMX219"):
            return
        if not self._check_motion_conflict("IMX219"):
            return
        try:
            n     = int(self.var_burst_n.get())
            delay = float(self.var_burst_delay.get())
            if n < 1:
                raise ValueError
        except (ValueError, tk.TclError):
            self.popup_info("Rafale", "Nombre et délai invalides.")
            return
        nom_base = self.askstring_kp(
            "Rafale", "Nom de base (vide = défaut) :")
        if nom_base is None:
            return
        nom_base = nom_base.strip() or None
        awb_mode, awb_gains = self._get_awb()
        self.label_info_219.config(text=f"Rafale {n} photos…")
        def _run():
            last = None
            def log(msg):
                self.root.after(0, lambda m=msg:
                                self.label_info_219.config(text=m))
            files = self.camera_ctrl.rafale_imx219(
                n=n, delai_s=delay,
                nom_base=nom_base,
                rotation=self._get_rotation(),
                brightness=self._get_brightness(),
                contrast=self._get_contrast(),
                saturation=self._get_saturation(),
                quality=self._get_quality(),
                shutter_us=self._get_shutter(),
                gain=self._get_gain(),
                awb_mode=awb_mode,
                awb_gains=awb_gains,
                log_cb=log,
            )
            if files:
                last = files[-1]
                self.root.after(0, lambda: self._update_thumbnail(
                    last, "IMX219"))
        threading.Thread(target=_run, daemon=True).start()

    def _video_imx219(self):
        if not self._check_cam0("IMX219"):
            return
        if not self._check_motion_conflict("IMX219"):
            return
        duree_str = self.askstring_kp(
            "Durée vidéo", "Durée en secondes (0 = continu) :")
        if duree_str is None:
            return
        duree = 0
        if duree_str.strip():
            try:
                duree = int(duree_str.strip())
            except ValueError:
                self.popup_info("...", "Veuillez entrer un nombre valide.")
                return
        if duree == 0:
            self.process_continu = self.camera_ctrl.faire_video(
                "0", "IMX219", duree_s=0)
            return
        nom = self.askstring_kp("Fichier", "Nom (vide = défaut) :")
        if nom is None:
            return
        self.camera_ctrl.faire_video(
            "0", "IMX219", duree_s=duree, nom=nom.strip() or None)

    # ===========================================================
    # Actions IMX708
    # ===========================================================
    def _photo_imx708(self, mode):
        if not self._check_cam0("IMX708"):
            return
        if not self._check_motion_conflict("IMX708"):
            return
        info = ("4608 × 2592 (~2 Mo) · 120°"
                if mode == "large" else "2304 × 1296 (< 1 Mo) · 120°")
        self.label_info_708.config(text=info)
        nom = self.askstring_kp("Fichier",
                                "Nom du fichier (vide = défaut) :")
        if nom is None:
            self.label_info_708.config(text="")
            return
        nom = nom.strip() or None
        self.label_info_708.config(text="Capture en cours…")
        awb_mode, awb_gains = self._get_awb()
        def _capture():
            f = self.camera_ctrl.prendre_photo_imx708(
                resolution=mode, nom=nom,
                rotation=self._get_rotation(),
                brightness=self._get_brightness(),
                contrast=self._get_contrast(),
                saturation=self._get_saturation(),
                quality=self._get_quality(),
                hdr=self.var_hdr.get(),
                lens_position=self._get_lens(),
                shutter_us=self._get_shutter(),
                gain=self._get_gain(),
                awb_mode=awb_mode,
                awb_gains=awb_gains,
            )
            if f:
                self.root.after(0, lambda: self.label_info_708.config(
                    text="✅ Capture terminée."))
                self.root.after(0, lambda: self._update_thumbnail(f, "IMX708"))
            else:
                self.root.after(0, lambda: self.label_info_708.config(
                    text="❌ Échec capture – vérifiez la caméra.",
                    foreground="red"))
        threading.Thread(target=_capture, daemon=True).start()

    def _rafale_imx708(self):
        if not self._check_cam0("IMX708"):
            return
        if not self._check_motion_conflict("IMX708"):
            return
        try:
            n     = int(self.var_burst_n.get())
            delay = float(self.var_burst_delay.get())
            if n < 1:
                raise ValueError
        except (ValueError, tk.TclError):
            self.popup_info("Rafale", "Nombre et délai invalides.")
            return
        nom_base = self.askstring_kp(
            "Rafale", "Nom de base (vide = défaut) :")
        if nom_base is None:
            return
        nom_base = nom_base.strip() or None
        awb_mode, awb_gains = self._get_awb()
        self.label_info_708.config(text=f"Rafale {n} photos…")
        def _run():
            def log(msg):
                self.root.after(0, lambda m=msg:
                                self.label_info_708.config(text=m))
            files = self.camera_ctrl.rafale_imx708(
                n=n, delai_s=delay,
                nom_base=nom_base,
                rotation=self._get_rotation(),
                brightness=self._get_brightness(),
                contrast=self._get_contrast(),
                saturation=self._get_saturation(),
                quality=self._get_quality(),
                hdr=self.var_hdr.get(),
                lens_position=self._get_lens(),
                shutter_us=self._get_shutter(),
                gain=self._get_gain(),
                awb_mode=awb_mode,
                awb_gains=awb_gains,
                log_cb=log,
            )
            if files:
                last = files[-1]
                self.root.after(0, lambda: self._update_thumbnail(
                    last, "IMX708"))
        threading.Thread(target=_run, daemon=True).start()

    def _video_imx708(self):
        if not self._check_cam0("IMX708"):
            return
        if not self._check_motion_conflict("IMX708"):
            return
        duree_str = self.askstring_kp(
            "Durée vidéo", "Durée en secondes (0 = continu) :")
        if duree_str is None:
            return
        duree = 0
        if duree_str.strip():
            try:
                duree = int(duree_str.strip())
            except ValueError:
                self.popup_info("...", "Veuillez entrer un nombre valide.")
                return
        if duree == 0:
            self.process_continu = self.camera_ctrl.faire_video(
                "0", "IMX708W", duree_s=0)
            return
        nom = self.askstring_kp("Fichier",
                                "Nom du fichier (vide = défaut) :")
        if nom is None:
            return
        self.camera_ctrl.faire_video(
            "0", "IMX708W", duree_s=duree, nom=nom.strip() or None)

    # ===========================================================
    # Actions IMX500
    # ===========================================================
    def _photo_imx500(self):
        nom = self.askstring_kp("Fichier",
                                "Nom du fichier (vide = défaut) :")
        if nom is None:
            return
        nom = nom.strip() or None
        cleanup_imx500()
        def _run():
            f = self.camera_ctrl.prendre_photo_imx500(nom=nom)
            if f:
                print(f"[IMX500] Photo enregistrée : {f}")
            else:
                print("[IMX500] Échec capture IMX500.")
        threading.Thread(target=_run, daemon=True).start()

    def _video_imx500_simple(self):
        duree_str = self.askstring_kp(
            "Durée vidéo", "Durée en secondes (0 = continu) :")
        if duree_str is None:
            return
        duree = 0
        if duree_str.strip():
            try:
                duree = int(duree_str.strip())
            except ValueError:
                self.popup_info("...", "Veuillez entrer un nombre valide.")
                return
        cleanup_imx500()
        if duree == 0:
            self.process_continu = self.camera_ctrl.faire_video(
                "1", "IMX500", duree_s=0)
            return
        nom = self.askstring_kp("Fichier",
                                "Nom du fichier (vide = défaut) :")
        if nom is None:
            return
        threading.Thread(
            target=lambda: self.camera_ctrl.faire_video(
                "1", "IMX500", duree_s=duree,
                nom=nom.strip() or None),
            daemon=True).start()

    def _video_imx500_ia(self):
        duree_str = self.askstring_kp(
            "Vidéo IA", "Durée en secondes (minimum 5) :")
        if duree_str is None:
            return
        if not duree_str.strip():
            self.popup_info("...", "Veuillez entrer un nombre.")
            return
        try:
            duree = int(duree_str.strip())
        except ValueError:
            self.popup_info("...", "Veuillez entrer un nombre valide.")
            return
        if duree < 5:
            self.popup_info("Attention", "La durée minimale est de 5 s.")
            return
        nom = self.askstring_kp("Fichier",
                                "Nom du fichier (vide = défaut) :")
        if nom is None:
            return
        nom  = nom.strip() or None
        conf = self.var_ia_confidence.get()
        cleanup_imx500()
        threading.Thread(
            target=lambda: self.ia_ctrl.ia_video(
                duree_s=duree + 5, nom=nom, confidence=conf),
            daemon=True).start()

    def _lancer_detection_objets(self):
        conf = self.var_ia_confidence.get()
        if self.var_ia_log_active.get():
            self.popup_info(
                "Log IA – information",
                "Les modes 'Détection d'objets' & 'PoseNet' utilisent\n"
                "rpicam-hello : non compatible avec le Log CSV.\n\n"
                "Utilisez '⚡ Multi-Processing'... pour le Log.")
            return
        self.lancer_continu(lambda: self.ia_ctrl.ia_detection_objets(conf),
                            delay_ms=20000)

    def lancer_continu(self, func, delay_ms=20000):
        if is_imx500_busy():
            self.popup_info("Caméra occupée",
                            "IMX500 déjà utilisée.\nNettoyage automatique…")
            cleanup_imx500()
        attente = self.popup_attente("Chargement IA...\nVeuillez patienter.")
        self.root.update()
        self.process_continu = func()
        self.root.after(delay_ms, attente.destroy)

    def arreter_continu(self):
        p = self.process_continu
        if p is None:
            return
        try:
            if p.poll() is None:
                p.terminate()
        except Exception:
            pass
        self.process_continu = None

    # ── Log IA ──
    def _ia_log_toggle(self):
        if self.var_ia_log_active.get():
            path = self.var_ia_log_path.get().strip()
            fmt  = self.var_ia_log_fmt.get()
            if not path:
                self.popup_info("Log IA",
                                "Veuillez spécifier un chemin de fichier.")
                self.var_ia_log_active.set(False)
                return
            self.ia_ctrl.start_log(path, fmt)
        else:
            self.ia_ctrl.stop_log()
        self._ia_log_coloriser_boutons()

    def _ia_log_coloriser_boutons(self):
        log_actif = self.var_ia_log_active.get()
        for btn, log_ok in self._ia_btns:
            if not log_actif:
                btn.config(bg=btn._neutral_bg, fg=btn._neutral_fg)
            elif log_ok:
                btn.config(bg="#2e7d32", fg="white")   # vert foncé
            else:
                btn.config(bg="#9e9e9e", fg="#eeeeee")  # gris

    def _ia_log_choose_path(self):
        fmt  = self.var_ia_log_fmt.get()
        ext  = ".csv" if fmt == "csv" else ".json"
        path = filedialog.asksaveasfilename(
            title="Fichier log détections IA",
            defaultextension=ext,
            initialfile=f"ia_detections{ext}",
            filetypes=[(f"Fichier {fmt.upper()}", f"*{ext}"),
                       ("Tous", "*.*")],
        )
        if path:
            self.var_ia_log_path.set(path)

    # ===========================================================
    # Vérification nappe
    # ===========================================================
    def _check_cam0(self, expected: str) -> bool:
        actual = self.var_cam0.get()
        if actual != expected:
            self.popup_info(
                "⚠ Attention – Nappe",
                f"L'onglet actif est {expected} mais le sélecteur "
                f"indique {actual}.\n"
                f"Vérifiez que la nappe {expected} est bien connectée "
                f"sur le port 0.")
            return False
        return True

    # ===========================================================
    # Vérification conflit Motion / caméra port 0
    # ===========================================================
    def _check_motion_conflict(self, cam_label: str) -> bool:
        """Avertit si Motion est actif et utilise le port 0 (IMX219 ou IMX708).
        Retourne True si l'utilisateur confirme vouloir continuer malgré tout,
        False s'il annule (la capture doit être abandonnée)."""
        if not MotionController.is_running():
            return True
        cam_motion = self.var_cam0.get()
        reponse = messagebox.askyesno(
            "⚠ Conflit caméra – Motion actif",
            f"Le service Motion est actuellement actif et occupe\n"
            f"le port caméra 0 ({cam_motion}).\n\n"
            f"Une capture {cam_label} risque d'échouer\n"
            f"(fichier absent ou corrompu).\n\n"
            f"Conseillé : arrêtez Motion depuis l'onglet « Motion »\n"
            f"avant de capturer.\n\n"
            f"Continuer quand même ?",
        )
        return reponse

    # ===========================================================
    # Actions Motion
    # ===========================================================
    def _update_motion_cam_label(self):
        self.label_motion_cam.config(
            text=f"Caméra active sur port 0 : {self.var_cam0.get()}")

    def _update_mode_label(self):
        mode = self.motion_mode.get()
        if mode == "enregistrement":
            self.label_mode_actif.config(
                text="Mode actif : 🎥 Enregistrement mkv  "
                     "(movie_output On)")
            self.btn_mode_enreg.config(relief="sunken")
            self.btn_mode_flux.config(relief="raised")
        else:
            self.label_mode_actif.config(
                text="Mode actif : 📡 Flux continu  "
                     "(movie_output Off)")
            self.btn_mode_enreg.config(relief="raised")
            self.btn_mode_flux.config(relief="sunken")

    def _motion_mode_enregistrement(self):
        """Passe en mode enregistrement MKV et redémarre Motion."""
        self.motion_mode.set("enregistrement")
        self._update_mode_label()
        ok = MotionController.set_movie_output(True)
        if not ok:
            self.popup_info(
                "Motion – Mode",
                "Impossible d'écrire dans motion.conf.\n"
                "Vérifiez les permissions (sudo).")
            return
        if MotionController.is_running():
            self._motion_restart()

    def _motion_mode_flux(self):
        """Passe en mode flux continu (sans enregistrement) et redémarre Motion."""
        self.motion_mode.set("flux")
        self._update_mode_label()
        ok = MotionController.set_movie_output(False)
        if not ok:
            self.popup_info(
                "Motion – Mode",
                "Impossible d'écrire dans motion.conf.\n"
                "Vérifiez les permissions (sudo).")
            return
        if MotionController.is_running():
            self._motion_restart()



    def _update_motion_status(self):
        running = MotionController.is_running()
        if running:
            self.label_motion_status.config(
                text="● Motion est ACTIF", foreground="green")
        else:
            self.label_motion_status.config(
                text="○ Motion est ARRÊTÉ", foreground="red")
        state = "normal" if running else "disabled"
        if self.btn_web:
            self.btn_web.config(state=state)
        if self.btn_stream:
            self.btn_stream.config(state=state)

    def _motion_start(self):
        self.label_motion_status.config(
            text="⏳ Démarrage en cours…", foreground="#aa6600")
        self.root.update()
        def _run():
            ok, msg = MotionController.demarrer()
            self.root.after(0, self._update_motion_status)
            if not ok:
                self.root.after(0, lambda: self.popup_info(
                    "Motion – Démarrage", f"Erreur : {msg}"))
        threading.Thread(target=_run, daemon=True).start()

    def _motion_stop(self):
        self.label_motion_status.config(
            text=f"⏳ Arrêt en cours (délai : {MOTION_STOP_GRACE} s)…",
            foreground="#aa6600")
        self.root.update()
        def _run():
            ok, msg = MotionController.arreter_proprement()
            self.root.after(0, self._update_motion_status)
            if not ok:
                self.root.after(0, lambda: self.popup_info(
                    "Motion – Arrêt", f"Erreur : {msg}"))
        threading.Thread(target=_run, daemon=True).start()

    def _motion_restart(self):
        self.label_motion_status.config(
            text="⏳ Redémarrage en cours…", foreground="#aa6600")
        self.root.update()
        def _run():
            ok, msg = MotionController.redemarrer()
            self.root.after(0, self._update_motion_status)
            if not ok:
                self.root.after(0, lambda: self.popup_info(
                    "Motion – Redémarrage", f"Erreur : {msg}"))
        threading.Thread(target=_run, daemon=True).start()

    def _open_web_if_running(self):
        if MotionController.is_running():
            MotionController.ouvrir_interface_web()
        else:
            self.popup_info("Motion",
                            "Le service Motion n'est pas démarré.")

    def _open_stream_if_running(self):
        if MotionController.is_running():
            MotionController.ouvrir_stream()
        else:
            self.popup_info("Motion",
                            "Le service Motion n'est pas démarré.")

    def _raz_motion_captures(self):
        directory   = MOTION_CAPTURES_DIR
        was_running = MotionController.is_running()
        if was_running:
            MotionController.arreter_proprement()
        try:
            for f in os.listdir(directory):
                path = os.path.join(directory, f)
                if os.path.isfile(path):
                    try:
                        os.remove(path)
                    except Exception:
                        pass
        except Exception:
            pass
        if was_running:
            MotionController.demarrer()
        self.label_motion_status.config(
            text="motion_captures nettoyé.", foreground="blue")

    def _motion_reparer_videos(self):
        directory = MOTION_CAPTURES_DIR
        self.motion_repair_log.config(text="Réparation en cours…",
                                      foreground="#aa6600")
        self.root.update()
        def _run():
            nb = MotionController.reparer_videos(directory)
            résumé = (f"✅ {nb} fichier(s) réparé(s)."
                      if nb > 0 else "Aucun fichier à réparer.")
            self.root.after(0, lambda: self.motion_repair_log.config(
                text=résumé,
                foreground="green" if nb > 0 else "#555555"))
        threading.Thread(target=_run, daemon=True).start()

    # ===========================================================
    # Actions Timelapse
    # ===========================================================
    def _tl_log(self, msg: str):
        def _write():
            self.tl_log.config(state="normal")
            self.tl_log.insert("end", msg + "\n")
            self.tl_log.see("end")
            self.tl_log.config(state="disabled")
        self.root.after(0, _write)

    def _tl_update_info(self):
        try:
            interval = max(1, self.tl_interval.get())
            duration = self.tl_duration.get()
            fps      = max(1, self.tl_fps.get())
            w        = self.tl_width.get()
            h        = self.tl_height.get()

            # +1 car la 1ère photo est prise immédiatement à t=0,
            # puis une photo toutes les "interval" secondes
            nb_frames  = (duration * 60) // interval + 1
            duree_vid  = nb_frames / fps

            self.tl_label_info.config(
                text=f"≈ {nb_frames} photos  →  "
                     f"vidéo ≈ {duree_vid:.1f} s à {fps} fps")

            # Estimations disque
            est = TimelapseController.estimer_taille(
                w, h, nb_frames, fps)
            self.tl_lbl_est_frames.config(
                text=f"📷 Frames JPG  : "
                     f"{est['frames_mo_min']:.0f} – "
                     f"{est['frames_mo_max']:.0f} Mo  "
                     f"({nb_frames} × {w}×{h})")
            self.tl_lbl_est_video.config(
                text=f"🎬 Vidéo MP4   : "
                     f"{est['video_mo_min']:.0f} – "
                     f"{est['video_mo_max']:.0f} Mo  "
                     f"({duree_vid:.1f} s · libx264 CRF23)")
            total_min = est['total_mo_min']
            total_max = est['total_mo_max']
            self.tl_lbl_est_total.config(
                text=f"📊 Total estimé : "
                     f"{total_min:.0f} – {total_max:.0f} Mo")

            # Espace disque libre
            try:
                out_dir = self.tl_output_dir.get() or DEFAULT_SAVE_DIR
                usage   = shutil.disk_usage(out_dir)
                libre   = usage.free / 1024**2
                color   = ("#cc0000" if libre < total_max
                           else ("#cc6600" if libre < total_max * 2
                                 else "#007700"))
                self.tl_lbl_est_disk.config(
                    text=f"💾 Espace libre : {libre:.0f} Mo "
                         f"sur partition cible",
                    foreground=color)
            except Exception:
                self.tl_lbl_est_disk.config(
                    text="💾 Espace libre : N/A",
                    foreground="#555555")

            # Reprise de session disponible ?
            out_dir = self.tl_output_dir.get()
            if TimelapseController.has_resume(out_dir):
                state = self._tl_load_resume(out_dir)
                self.tl_btn_resume.config(state="normal")
            else:
                self.tl_btn_resume.config(state="disabled")
                self.tl_lbl_resume.config(
                    text="Aucune session interrompue détectée.")
        except Exception:
            self.tl_label_info.config(text="")

    def _tl_load_resume(self, out_dir):
        info = TimelapseController.get_resume_info(out_dir)
        if info:
            frames = info.get("frame_index", "?")
            elapsed = info.get("elapsed_s", 0)
            self.tl_lbl_resume.config(
                text=f"Session trouvée : {frames} frames, "
                     f"{elapsed:.0f} s écoulées.")
        return info

    def _tl_awb_lock_changed(self):
        state = "normal" if self.tl_awb_lock.get() else "disabled"
        self.tl_entry_r.config(state=state)
        self.tl_entry_b.config(state=state)

    def _tl_choose_dir(self):
        rep = filedialog.askdirectory(
            title="Dossier de stockage des photos Timelapse")
        if rep:
            self.tl_output_dir.set(rep)
            self._tl_update_info()

    def _tl_preview_start(self):
        self._tl_preview_stop()
        camera = self.tl_var_camera.get()
        if camera == "IMX708" and not self._check_cam0("IMX708"):
            return
        if camera == "IMX500":
            cleanup_imx500()
        cam_index = "1" if camera == "IMX500" else "0"
        cmd = ["rpicam-hello", "-t", "0", "--camera", cam_index]
        self._tl_log(
            f"Prévisualisation {camera} lancée\n"
            f"(fermez la fenêtre ou cliquez sur Arrêter).")
        try:
            self._preview_process = subprocess.Popen(cmd)
        except Exception as e:
            self._tl_log(f"Erreur prévisualisation : {e}")

    def _tl_preview_stop(self):
        p = self._preview_process
        if p is not None:
            try:
                if p.poll() is None:
                    p.terminate()
            except Exception:
                pass
            self._preview_process = None
            self._tl_log("Prévisualisation arrêtée.")

    def _tl_start(self):
        camera   = self.tl_var_camera.get()
        if camera == "IMX708" and not self._check_cam0("IMX708"):
            return
        interval = self.tl_interval.get()
        duration = self.tl_duration.get()
        if interval <= 0 or duration <= 0:
            self.popup_info("Timelapse",
                            "L'intervalle et la durée doivent être > 0.")
            return
        self._tl_preview_stop()
        output_dir = self.tl_output_dir.get()

        # AWB gains
        awb_lock = self.tl_awb_lock.get()
        awb_gains = None
        if awb_lock:
            try:
                r = float(self.tl_awb_r.get())
                b = float(self.tl_awb_b.get())
                awb_gains = (r, b)
            except Exception:
                awb_gains = None

        self.tl_btn_start.config(state="disabled")
        self.tl_btn_stop.config(state="normal")
        self.tl_label_status.config(
            text=f"● En cours – {camera}  "
                 f"({interval} s / {duration} min)",
            foreground="green")
        self._tl_log(
            f"--- Démarrage timelapse {camera} – "
            f"intervalle {interval} s – durée {duration} min ---")
        self._tl_log(f"Dossier : {output_dir}")
        if awb_lock:
            self._tl_log(
                f"AWB lock actif – gains : "
                f"R={awb_gains[0] if awb_gains else 'off'} "
                f"B={awb_gains[1] if awb_gains else 'off'}")
        if camera == "IMX500":
            cleanup_imx500()
        self.timelapse_ctrl.demarrer(
            camera=camera, interval_s=interval, duration_min=duration,
            width=self.tl_width.get(), height=self.tl_height.get(),
            output_dir=output_dir, hdr=self.tl_hdr.get(),
            awb_lock=awb_lock, awb_gains=awb_gains,
            resume=False, log_cb=self._tl_log,
        )
        self.root.after(1000, self._tl_watch)

    def _tl_resume(self):
        """Reprend une session timelapse interrompue."""
        output_dir = self.tl_output_dir.get()
        info = TimelapseController.get_resume_info(output_dir)
        if not info:
            self.popup_info("Reprise", "Aucune session à reprendre.")
            return
        camera   = info.get("camera", "IMX708")
        interval = info.get("interval_s", 30)
        duration = info.get("duration_min", 10)
        width    = info.get("width", 2304)
        height   = info.get("height", 1296)
        hdr      = info.get("hdr", False)
        awb_lock = info.get("awb_lock", False)
        gains    = info.get("awb_gains")
        awb_gains = tuple(gains) if gains else None

        self.tl_btn_start.config(state="disabled")
        self.tl_btn_stop.config(state="normal")
        self.tl_label_status.config(
            text=f"● Reprise – {camera}", foreground="green")
        self._tl_log(f"--- Reprise de session {camera} ---")
        if camera == "IMX500":
            cleanup_imx500()
        self.timelapse_ctrl.demarrer(
            camera=camera, interval_s=interval, duration_min=duration,
            width=width, height=height, output_dir=output_dir,
            hdr=hdr, awb_lock=awb_lock, awb_gains=awb_gains,
            resume=True, log_cb=self._tl_log,
        )
        self.root.after(1000, self._tl_watch)

    def _tl_watch(self):
        if self.timelapse_ctrl.capturing:
            self.root.after(1000, self._tl_watch)
        else:
            self.tl_btn_start.config(state="normal")
            self.tl_btn_stop.config(state="disabled")
            self.tl_label_status.config(text="Terminé.",
                                        foreground="#555555")
            self._tl_update_info()

    def _tl_stop(self):
        self.timelapse_ctrl.arreter()
        self.tl_label_status.config(text="Arrêt demandé…",
                                    foreground="orange")
        self._tl_log("Arrêt demandé par l'utilisateur.")

    def _tl_create_video(self):
        output_dir = self.tl_output_dir.get()
        fps        = self.tl_fps.get()
        video_path = filedialog.asksaveasfilename(
            title="Enregistrer la vidéo MP4",
            defaultextension=".mp4",
            initialfile=f"timelapse_{timestamp()}.mp4",
            filetypes=[("Vidéo MP4", "*.mp4")],
        )
        if not video_path:
            return
        threading.Thread(
            target=TimelapseController.creer_video,
            args=(output_dir, fps, video_path, self._tl_log),
            daemon=True,
        ).start()

    # ===========================================================
    # Helpers Slow Motion
    # ===========================================================
    def _sm_log(self, msg: str):
        def _write():
            self.sm_log.config(state="normal")
            self.sm_log.insert("end", msg + "\n")
            self.sm_log.see("end")
            self.sm_log.config(state="disabled")
        self.root.after(0, _write)

    def _sm_update_facteur(self):
        idx = self.sm_var_mode.get()
        try:
            _, _, _, fps = SM_CAPTURE_MODES[idx]
            facteur = fps / SM_OUTPUT_FPS
            self.sm_label_facteur.config(
                text=f"Capture à {fps} fps  ·  ralenti × "
                     f"{facteur:.1f}  en sortie à {SM_OUTPUT_FPS} fps")
        except Exception:
            self.sm_label_facteur.config(text="")

    def _sm_update_conv_facteur(self):
        try:
            fps_src = self.sm_var_fps_src.get()
            fps_tgt = self.sm_var_fps_tgt.get()
            if fps_tgt > 0 and fps_src > 0:
                facteur = fps_src / fps_tgt
                self.sm_label_conv_facteur.config(
                    text=f"Facteur ralenti : × {facteur:.1f}  "
                         f"({fps_src} fps → {fps_tgt} fps)")
            else:
                self.sm_label_conv_facteur.config(text="")
        except Exception:
            self.sm_label_conv_facteur.config(text="")

    def _sm_choose_dir(self):
        rep = filedialog.askdirectory(title="Dossier de sauvegarde HFR")
        if rep:
            self.sm_var_dir.set(rep)
            self.camera_ctrl.save_dir = rep

    def _sm_choose_source(self):
        f = filedialog.askopenfilename(
            title="Choisir la vidéo source",
            filetypes=[("Vidéos", "*.mp4 *.avi *.mkv *.mov *.h264"),
                       ("Tous", "*.*")],
        )
        if f:
            self.sm_var_src.set(f)
            fps = SlowMotionController.detecter_fps(f)
            if fps:
                self.sm_var_fps_src.set(int(round(fps)))
                self.sm_label_fps_detect.config(
                    text=f"(détecté : {fps:.2f} fps)")
            else:
                self.sm_label_fps_detect.config(
                    text="(détection impossible)")

    def _sm_detecter_fps(self):
        src = self.sm_var_src.get().strip()
        if not src:
            self.popup_info("Slow Motion",
                            "Veuillez d'abord choisir une vidéo source.")
            return
        fps = SlowMotionController.detecter_fps(src)
        if fps:
            self.sm_var_fps_src.set(int(round(fps)))
            self.sm_label_fps_detect.config(
                text=f"(détecté : {fps:.2f} fps)")
            self._sm_log(f"FPS détecté : {fps:.2f} fps")
        else:
            self.sm_label_fps_detect.config(text="(détection échouée)")
            self._sm_log("❌ Impossible de détecter les FPS.")

    def _sm_capturer_hfr(self):
        cam = self.var_cam0.get()
        idx = self.sm_var_mode.get()
        label_mode, w, h, fps = SM_CAPTURE_MODES[idx]
        if w == 1280 and cam != "IMX708":
            self.popup_info(
                "⚠ Mode 60 fps 1280×720",
                "Ce mode nécessite la caméra IMX708.\n"
                "Vérifiez que la nappe est connectée sur IMX708.")
            return
        try:
            duree = int(self.sm_var_duree.get())
            if duree <= 0:
                raise ValueError
        except (ValueError, tk.TclError):
            self.popup_info("Slow Motion",
                            "Durée invalide (entrez un entier > 0).")
            return
        nom = self.askstring_kp("Fichier HFR",
                                "Nom du fichier (vide = défaut) :")
        if nom is None:
            return
        nom     = nom.strip() or None
        facteur = fps / SM_OUTPUT_FPS
        self._sm_log(
            f"--- Capture HFR : {w}×{h} @ {fps} fps – "
            f"durée {duree} s  (ralenti × {facteur:.1f}) ---")
        def _run():
            fichier = self.camera_ctrl.capturer_hfr(
                width=w, height=h, fps=fps, duree_s=duree, nom=nom)
            self.root.after(0, lambda: self._sm_log(
                f"✅ Capture terminée : {fichier}"))
            self.root.after(0, lambda: self.sm_var_src.set(fichier))
            self.root.after(0, lambda: self.sm_var_fps_src.set(fps))
            self.root.after(0, lambda: self.sm_label_fps_detect.config(
                text=f"(capturée à {fps} fps)"))
        threading.Thread(target=_run, daemon=True).start()

    def _sm_ouvrir_vlc(self):
        f = filedialog.askopenfilename(
            title="Choisir une vidéo à lire dans VLC",
            filetypes=[("Vidéos", "*.mp4 *.avi *.mkv *.mov *.h264"),
                       ("Tous", "*.*")],
        )
        if f:
            self._sm_log(f"Ouverture dans VLC : {os.path.basename(f)}")
            SlowMotionController.ouvrir_vlc(f)

    def _sm_convertir(self):
        src = self.sm_var_src.get().strip()
        if not src or not os.path.isfile(src):
            self.popup_info("Slow Motion",
                            "Veuillez choisir une vidéo source valide.")
            return
        try:
            fps_src = int(self.sm_var_fps_src.get())
            fps_tgt = int(self.sm_var_fps_tgt.get())
            if fps_src <= 0 or fps_tgt <= 0:
                raise ValueError
        except (ValueError, tk.TclError):
            self.popup_info("Slow Motion",
                            "FPS source et FPS sortie doivent être > 0.")
            return
        methode    = self.sm_var_methode.get()
        nom_defaut = (f"slowmo_"
                      f"{os.path.splitext(os.path.basename(src))[0]}"
                      f"_{timestamp()}.mp4")
        output = filedialog.asksaveasfilename(
            title="Enregistrer la vidéo ralenti",
            defaultextension=".mp4",
            initialfile=nom_defaut,
            filetypes=[("Vidéo MP4", "*.mp4")],
        )
        if not output:
            return
        facteur = fps_src / fps_tgt
        self._sm_log(
            f"--- Conversion {methode}  × {facteur:.1f}  "
            f"({fps_src} fps → {fps_tgt} fps) ---")
        def _run():
            if methode == "setpts":
                SlowMotionController.convertir_setpts(
                    src, output, fps_src, fps_tgt,
                    log_cb=self._sm_log)
            else:
                SlowMotionController.convertir_minterpolate(
                    src, output, fps_src, fps_tgt,
                    log_cb=self._sm_log)
        threading.Thread(target=_run, daemon=True).start()

    # ===========================================================
    # Actions Fichiers
    # ===========================================================
    def _fich_refresh(self):
        """Recharge la liste des fichiers du dossier de sauvegarde."""
        for item in self.fich_tree.get_children():
            self.fich_tree.delete(item)
        save_dir = self.camera_ctrl.save_dir
        try:
            entries = sorted(
                (e for e in os.scandir(save_dir) if e.is_file() and not e.name.startswith(".")),
                key=lambda e: e.stat().st_mtime,
                reverse=True,
            )
            for e in entries:
                st   = e.stat()
                size = st.st_size
                if size < 1024:
                    taille = f"{size} o"
                elif size < 1024**2:
                    taille = f"{size/1024:.1f} Ko"
                else:
                    taille = f"{size/1024**2:.1f} Mo"
                date = datetime.datetime.fromtimestamp(
                    st.st_mtime).strftime("%Y-%m-%d  %H:%M:%S")
                self.fich_tree.insert(
                    "", "end",
                    iid=e.path,
                    values=(e.name, taille, date))
        except Exception as err:
            self.fich_tree.insert("", "end",
                                   values=(f"Erreur : {err}", "", ""))

    def _fich_open_file(self, event=None):
        sel = self.fich_tree.selection()
        if sel:
            path = sel[0]
            try:
                subprocess.Popen(["xdg-open", path])
            except Exception:
                pass

    def _fich_open_manager(self):
        try:
            subprocess.Popen(
                ["xdg-open", self.camera_ctrl.save_dir])
        except Exception:
            pass

    def _fich_delete(self):
        sel = self.fich_tree.selection()
        if not sel:
            return
        noms = [os.path.basename(p) for p in sel]
        msg  = (f"Supprimer {len(sel)} fichier(s) ?\n"
                + "\n".join(noms[:5])
                + ("\n…" if len(noms) > 5 else ""))
        if not messagebox.askyesno("Confirmer suppression", msg):
            return
        for path in sel:
            try:
                os.remove(path)
            except Exception:
                pass
        self._fich_refresh()

    # ===========================================================
    # Dossier de sauvegarde
    # ===========================================================
    def changer_repertoire(self):
        rep = filedialog.askdirectory(
            title="Choisir un dossier de sauvegarde")
        if not rep:
            return
        rep = os.path.abspath(rep)
        os.makedirs(rep, exist_ok=True)
        self.camera_ctrl.save_dir    = rep
        self.ia_ctrl.save_dir        = rep
        self.timelapse_ctrl.save_dir = rep
        self.sm_var_dir.set(rep)
        self.label_save_dir.config(text=rep)
        self._fich_refresh()
        self._tl_update_info()

# ============================================================
# Point d'entrée
# ============================================================
def main():
    camera_ctrl = CameraController(DEFAULT_SAVE_DIR)
    ia_ctrl     = IAController(DEFAULT_SAVE_DIR, IMX500_DEMOS)

    root = tk.Tk()
    root.withdraw()

    splash = tk.Toplevel(root)
    splash.title("Démarrage…")
    splash.geometry("350x200+500+300")
    splash.resizable(False, False)
    splash.attributes("-topmost", True)

    try:
        icon_path  = os.path.join(ICON_DIR, "Sony_Logo.png")
        img        = Image.open(icon_path).resize((250, 200))
        splash_img = ImageTk.PhotoImage(img)
        tk.Label(splash, image=splash_img).pack(pady=10)
        splash._splash_img = splash_img  
    except Exception as e:
        print(f"[Splash] Logo introuvable ou illisible : {e}")
        tk.Label(splash, text="Pilotage Caméras – Pi5",
                 font=("Arial", 14, "bold")).pack(pady=(30, 8))
    tk.Label(splash,
             text="Chargement du pilotage des caméras…").pack()
    root.update()

    def show_main():
        splash.destroy()
        root.deiconify()
        root.gui = CameraGUI(root, camera_ctrl, ia_ctrl)
        root.geometry("800x960")       # dimensionnement de la fenêtre
        
    root.after(2000, show_main)

    def on_close():
        if hasattr(root, "gui"):
            gui = root.gui
            # Arrêt prévisualisation timelapse
            if gui._preview_process is not None:
                try:
                    gui._preview_process.terminate()
                except Exception:
                    pass
            # Arrêt mode continu
            if gui.process_continu:
                try:
                    gui.process_continu.terminate()
                except Exception:
                    pass
            # pkill IMX500 uniquement si un process IA a été lancé
            if gui.process_continu is not None or (
                hasattr(gui, "ia_ctrl") and gui.ia_ctrl.log_ia_active
            ):
                cleanup_imx500()
            # Arrêt timelapse propre
            if hasattr(gui, "timelapse_ctrl") and gui.timelapse_ctrl.capturing:
                gui.timelapse_ctrl.arreter()
        else:
            # Fenêtre fermée avant la fin du splash : nettoyage minimal
            pass
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()

if __name__ == "__main__":
    main()
