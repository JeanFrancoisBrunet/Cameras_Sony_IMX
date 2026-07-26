# Pilotage Caméras Sony — Raspberry Pi 5

Application Tkinter de pilotage unifié de trois caméras Sony sur un Raspberry Pi 5 : **IMX219**, **IMX708 Wide** (autofocus 120°) et **IMX500** (caméra à IA embarquée). Photo, vidéo, détection Motion, timelapse anti-scintillement, slow motion HFR et explorateur de fichiers, le tout dans une seule interface à onglets.

## Configuration matérielle
- **Port Caméra 0** : IMX219 *ou* IMX708 (connexion en alternance sur la même nappe, sélectionnable dans l'interface)
- **Port Caméra 1** : IMX500 (caméra à intelligence artificielle embarquée, Sony)

## Onglets de l'application

### 📷 IMX219 / 🦉 IMX708 Wide
- Capture photo avec **exposition manuelle** (`--shutter` en µs, `--gain`) et **balance des blancs manuelle** (`--awbgains r,b`).
- Réglages communs : rotation, luminosité, contraste, saturation, qualité JPEG, HDR (selon caméra).
- IMX708 uniquement : contrôle de la mise au point (lentille, de l'infini à 10 dioptries).
- **Mode rafale** configurable (nombre de captures et délai entre chaque prise).
- Miniature de la dernière photo capturée, affichée directement dans l'interface.

### 🧠 IMX500 (IA)
Modes d'inférence embarquée disponibles :
- **Détection d'objets** (SSD MobileNet), seuil de confiance configurable.
- **Pose** (PoseNet) et **HigherHRNet** (estimation de pose alternative).
- **Segmentation** (DeepLabv3+).
- **Classification** (MobileNet v2, labels ImageNet).
- **Détection rapide** (variante allégée).
- **Vidéo IA** : enregistrement MP4 avec overlay de détection intégré.
- **Export du log des détections** en CSV ou JSON (horodatage, label, confiance, bbox), en complément de l'overlay `rpicam-app` natif.

### 🏃 Motion
- Pilotage du démon **Motion** (détection de mouvement) pour l'IMX219 *et* l'IMX708, via `systemctl` et `libcamerify`.
- Deux modes : **Enregistrement** (captures dans `motion_captures/`) ou **Flux** (streaming web sur `http://localhost:8081`), avec interface web de contrôle sur `http://localhost:8080`.
- Détection de conflit d'usage caméra entre Motion et les autres onglets.
- Réparation des vidéos Motion, remise à zéro du dossier de captures.

### ⏱ Timelapse
- Compatible IMX708 et/ou IMX500.
- **AWB lock + AE lock** (`--awb off --awbgains`) pour éliminer le scintillement entre les images d'une même série.
- **Reprise de session** après interruption, via un fichier d'état `resume_state.json` dans le dossier de sortie.
- **Watchdog** : arrêt automatique après un nombre configurable d'échecs de capture consécutifs.
- Estimations en temps réel de la taille des fichiers (JPEG par résolution, débit vidéo MP4 par résolution) et de l'espace disque disponible.
- Génération de la vidéo timelapse finale (MP4) à partir des images capturées.

### 🎬 Slow Motion
- Capture HFR (haute fréquence d'images, jusqu'à 120 fps) sur IMX219/IMX708.
- Détection automatique du fps source d'une vidéo existante.
- Conversion en ralenti par deux méthodes au choix : **setpts** (ffmpeg, rapide) ou **minterpolate** (interpolation de frames, plus fluide mais plus lent).
- Lecture directe dans **VLC**.

### 📁 Fichiers
- Explorateur minimaliste du dossier de sauvegarde (nom, taille, date), tri par date de modification.
- Ouverture, suppression (avec confirmation) et accès au gestionnaire de fichiers du système.
- Changement du dossier de sauvegarde, répercuté sur tous les contrôleurs (photo, IA, timelapse, slow motion).

### Barre de statut système
- Température CPU du Pi5 et espace disque disponible, mis à jour en continu.

## Scripts de démonstration IMX500

Le dossier inclut les démonstrations officielles Sony/Raspberry Pi (`picamera2.devices.imx500`), lancées en sous-processus par l'onglet IMX500 et adaptées avec un export CSV optionnel des détections :
- **`imx500_object_detection_demo.py`** — détection d'objets (SSD / nanodet), boîtes englobantes et labels dessinés sur le flux ISP en direct.
- **`imx500_classification_demo.py`** — classification d'image (top 3 résultats), avec export CSV des détections via la variable d'environnement `IMX500_LOG_CSV` (throttlé à une ligne toutes les 5 secondes).
- **`imx500_segmentation_demo.py`** — segmentation sémantique (DeepLabv3+), masques colorés superposés au flux vidéo, avec le même export CSV.

Ces trois scripts peuvent aussi être lancés indépendamment en ligne de commande, avec leurs propres options (`--model`, `--threshold`, `--labels`, `--print-intrinsics`…).

## Lancement
Application principale :
```bash
python3 pilotage_cameras_PI5_v6.py
```

Démos IMX500 en ligne de commande (exemple) :
```bash
python3 imx500_object_detection_demo.py --threshold 0.6
python3 imx500_classification_demo.py --softmax
python3 imx500_segmentation_demo.py
```

## Dépendances
- Raspberry Pi OS (Bookworm) avec le firmware IMX500 installé (`/usr/share/imx500-models/`)
- `rpicam-apps` (`rpicam-hello`, `rpicam-vid`)
- `picamera2` (avec le module `picamera2.devices.imx500`)
- `motion` (détection de mouvement), `systemctl`, `libcamerify`
- `ffmpeg`, `vlc`
- Python : `opencv-python`, `numpy`, `pillow`
```bash
pip install opencv-python numpy pillow --break-system-packages
```

> Le script principal importe `motion_mode_manager` depuis un chemin absolu (`/home/jfbrunet/Projects/Telegram`), utilisé pour coordonner l'état du mode Motion avec le bot Telegram associé — à adapter selon l'installation.

## Structure du dépôt
```
Pilotage_Cameras_Sony/
├── pilotage_cameras_PI5_v6.py            # Application principale (Tkinter, tous les onglets)
├── imx500_object_detection_demo.py       # Démo IMX500 — détection d'objets
├── imx500_classification_demo.py         # Démo IMX500 — classification
├── imx500_segmentation_demo.py           # Démo IMX500 — segmentation
├── assets/                               # Labels et fichiers de post-traitement (coco_labels.txt, colours.txt…)
├── icons/                                # Logos et images d'illustration (non inclus ici)
└── motion_captures/                      # Captures du mode Motion (généré à l'exécution)
```

> `resume_state.json` (par session timelapse), le contenu de `motion_captures/`, ainsi que les logs de détection IA générés à l'exécution sont propres à chaque utilisation et n'ont pas vocation à être versionnés.

## Auteur
Jean-François BRUNET - JFBConseils - Juillet 2026
