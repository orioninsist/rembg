import io
import ipaddress
import json
import os
import socket
import tempfile
import warnings
import webbrowser
from typing import List, Optional, Tuple, cast
from urllib.parse import urlparse

import aiohttp
import click
import gradio as gr
import uvicorn
from asyncer import asyncify
from fastapi import Depends, FastAPI, File, Form, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import Response

from .. import __version__
from ..bg import remove
from ..session_factory import new_session
from ..sessions import sessions_names
from ..sessions.base import BaseSession

# Gradio 6 uyumluluk: theme/css uyarılarını bastır (mount_gradio_app ile kullanım)
warnings.filterwarnings("ignore", message=".*theme.*moved.*launch.*", category=UserWarning)
warnings.filterwarnings("ignore", message=".*css.*moved.*launch.*", category=UserWarning)

# ---------------------------------------------------------------------------
# Model metadata
# ---------------------------------------------------------------------------
MODEL_INFO = {
    "u2net":                ("🎯 Genel",        "Genel amaçlı, dengeli hız/kalite"),
    "u2netp":               ("⚡ Hızlı",         "En hızlı model, hafif görevler"),
    "u2net_human_seg":      ("👤 İnsan",         "İnsan / portre segmentasyonu"),
    "u2net_cloth_seg":      ("👗 Kıyafet",       "Kıyafet ve tekstil"),
    "silueta":              ("🖤 Siluet",        "Siluet çıkarma"),
    "birefnet-general":     ("✨ BiRefNet",      "Yüksek kalite genel"),
    "birefnet-general-lite":("✨ BiRefNet Lite", "Hızlı yüksek kalite"),
    "birefnet-portrait":    ("🧑 Portre",        "Yüz / insan odaklı"),
    "birefnet-dis":         ("🔬 Detaylı",       "Karmaşık sahneler"),
    "birefnet-hrsod":       ("📸 Yüksek Çöz.",  "Büyük resimler"),
    "birefnet-cod":         ("🦎 Kamuflaj",      "Kamuflajlı nesneler"),
    "birefnet-massive":     ("💪 Massive",       "En kapsamlı model"),
    "dis-general-use":      ("🌐 DIS",           "Detaylı kenarlıklar"),
    "dis-anime":            ("🎌 Anime",         "Anime / çizgi karakter"),
    "bria-rmbg":            ("🏢 BRIA",          "Ticari kalite"),
}

# Hız/Kalite presetleri
PRESETS = {
    "⚡ Çok Hızlı":     {"model": "u2netp",            "alpha": False, "fg": 240, "bg": 10, "erode": 10, "ppm": False},
    "🎯 Dengeli":       {"model": "u2net",             "alpha": False, "fg": 240, "bg": 10, "erode": 10, "ppm": True},
    "✨ Yüksek Kalite": {"model": "birefnet-general",  "alpha": True,  "fg": 240, "bg": 10, "erode": 15, "ppm": True},
    "🧑 Portre":        {"model": "birefnet-portrait", "alpha": True,  "fg": 240, "bg": 10, "erode": 10, "ppm": True},
    "🎌 Anime":         {"model": "dis-anime",         "alpha": False, "fg": 240, "bg": 10, "erode": 10, "ppm": True},
    "👤 İnsan":         {"model": "u2net_human_seg",   "alpha": True,  "fg": 240, "bg": 10, "erode": 10, "ppm": True},
    "👗 Kıyafet":       {"model": "u2net_cloth_seg",   "alpha": False, "fg": 240, "bg": 10, "erode": 10, "ppm": False},
}


def get_model_choices():
    return [(f"{v[0]} — {v[1]}", k) for k, v in MODEL_INFO.items() if k in sessions_names]


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------
@click.command(name="s", help="for a http server")
@click.option("-p", "--port",      default=7000,      type=int, show_default=True, help="port")
@click.option("-h", "--host",      default="0.0.0.0", type=str, show_default=True, help="host")
@click.option("-l", "--log_level", default="info",    type=str, show_default=True, help="log level")
@click.option("-t", "--threads",   default=None,      type=int, show_default=True, help="worker threads")
@click.option("--no-ui", is_flag=True, default=False, help="disable Gradio UI")
def s_command(port: int, host: str, log_level: str, threads: int, no_ui: bool) -> None:
    """FastAPI + Gradio web sunucusu."""

    sessions: dict[str, BaseSession] = {}

    # -------------------------------------------------------------------
    # FastAPI app
    # -------------------------------------------------------------------
    app = FastAPI(
        title="Rembg",
        description="AI Background Removal — Web UI & REST API",
        version=__version__,
        docs_url="/api",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_credentials=False,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -------------------------------------------------------------------
    # REST API parametreleri
    # -------------------------------------------------------------------
    class CommonQueryParams:
        def __init__(
            self,
            model: str = Query(
                default="u2net",
                pattern=r"(" + "|".join(sessions_names) + ")",
                description="Model adı",
            ),
            a:   bool = Query(default=False,  description="Alpha matting"),
            af:  int  = Query(default=240, ge=0, le=255, description="Ön plan eşiği"),
            ab:  int  = Query(default=10,  ge=0, le=255, description="Arka plan eşiği"),
            ae:  int  = Query(default=10,  ge=0,         description="Erozyon boyutu"),
            om:  bool = Query(default=False, description="Sadece maske"),
            ppm: bool = Query(default=False, description="Post-process mask"),
            bgc: Optional[str] = Query(default=None, description="Arka plan rengi R,G,B,A"),
            extras: Optional[str] = Query(default=None, description="Ek JSON parametreler"),
        ):
            self.model = model
            self.a = a; self.af = af; self.ab = ab; self.ae = ae
            self.om = om; self.ppm = ppm; self.extras = extras
            self.bgc = (
                cast(Tuple[int, int, int, int], tuple(map(int, bgc.split(","))))
                if bgc else None
            )

    class CommonQueryPostParams:
        def __init__(
            self,
            model: str = Form(
                default="u2net",
                pattern=r"(" + "|".join(sessions_names) + ")",
                description="Model adı",
            ),
            a:   bool = Form(default=False),
            af:  int  = Form(default=240, ge=0, le=255),
            ab:  int  = Form(default=10,  ge=0, le=255),
            ae:  int  = Form(default=10,  ge=0),
            om:  bool = Form(default=False),
            ppm: bool = Form(default=False),
            bgc: Optional[str]  = Query(default=None),
            extras: Optional[str] = Query(default=None),
        ):
            self.model = model
            self.a = a; self.af = af; self.ab = ab; self.ae = ae
            self.om = om; self.ppm = ppm; self.extras = extras
            self.bgc = (
                cast(Tuple[int, int, int, int], tuple(map(int, bgc.split(","))))
                if bgc else None
            )

    def _run_remove(content: bytes, commons) -> Response:
        kwargs: dict = {}
        if commons.extras:
            try:
                kwargs.update(json.loads(commons.extras))
            except Exception:
                pass
        session = sessions.get(commons.model)
        if session is None:
            session = new_session(commons.model, **kwargs)
            sessions[commons.model] = session
        return Response(
            remove(
                content,
                session=session,
                alpha_matting=commons.a,
                alpha_matting_foreground_threshold=commons.af,
                alpha_matting_background_threshold=commons.ab,
                alpha_matting_erode_size=commons.ae,
                only_mask=commons.om,
                post_process_mask=commons.ppm,
                bgcolor=commons.bgc,
                **kwargs,
            ),
            media_type="image/png",
        )

    @app.on_event("startup")
    def startup():
        try:
            webbrowser.open(f"http://localhost:{port}")
        except Exception:
            pass
        if threads is not None:
            from anyio import CapacityLimiter
            from anyio.lowlevel import RunVar
            RunVar("_default_thread_limiter").set(CapacityLimiter(threads))

    def _is_private_ip(h: str) -> bool:
        try:
            for item in socket.getaddrinfo(h, None):
                ip = ipaddress.ip_address(item[4][0])
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                    return True
        except Exception:
            return True
        return False

    def _validate_url(url: str) -> None:
        p = urlparse(url)
        if p.scheme not in ("http", "https"):
            raise ValueError("Only http/https URLs are allowed.")
        if not p.hostname:
            raise ValueError("Invalid URL: missing hostname.")
        if _is_private_ip(p.hostname):
            raise ValueError("Requests to private/internal addresses are not allowed.")

    @app.get(
        path="/api/remove",
        tags=["Background Removal"],
        summary="Remove from URL",
        description="Removes the background from an image obtained by URL.",
    )
    async def get_index(
        url: str = Query(default=..., description="Image URL"),
        commons: CommonQueryParams = Depends(),
    ):
        try:
            _validate_url(url)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        async with aiohttp.ClientSession() as s:
            async with s.get(url) as r:
                return await asyncify(_run_remove)(await r.read(), commons)

    @app.post(
        path="/api/remove",
        tags=["Background Removal"],
        summary="Remove from Stream",
        description="Removes the background from an uploaded image file.",
    )
    async def post_index(
        file: bytes = File(default=..., description="Image file bytes"),
        commons: CommonQueryPostParams = Depends(),
    ):
        return await asyncify(_run_remove)(file, commons)  # type: ignore

    # -------------------------------------------------------------------
    # Gradio UI (Gradio 6 tam uyumlu)
    # -------------------------------------------------------------------
    def gr_app(fastapi_app):

        def _get_session(model: str) -> BaseSession:
            s = sessions.get(model)
            if s is None:
                s = new_session(model)
                sessions[model] = s
            return s

        def _hex_to_rgba(hex_color: str, alpha: int = 255) -> Optional[Tuple[int, int, int, int]]:
            if not hex_color:
                return None
            h = hex_color.lstrip("#")
            if len(h) == 6:
                return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), alpha)
            return None

        def apply_preset(preset_name: str):
            """Preset seçilince model + ayarları otomatik doldurur."""
            if preset_name not in PRESETS:
                return (gr.update(),) * 7
            p = PRESETS[preset_name]
            return (
                p["model"],
                p["alpha"],
                p["fg"],
                p["bg"],
                p["erode"],
                p["ppm"],
                f"✅ Preset: **{preset_name}** — Model: `{p['model']}`",
            )

        def process_single(
            input_image, model, alpha, fg, bg, erode,
            only_mask, ppm, use_bgcolor, bgcolor_hex, bgcolor_alpha,
        ):
            if input_image is None:
                return None, None, "⚠️ Lütfen bir resim yükleyin."
            try:
                session = _get_session(model)
                bgcolor = None
                if use_bgcolor and bgcolor_hex:
                    bgcolor = _hex_to_rgba(bgcolor_hex, int(bgcolor_alpha))

                with open(input_image, "rb") as f:
                    data = f.read()

                out_bytes = remove(
                    data,
                    session=session,
                    alpha_matting=alpha,
                    alpha_matting_foreground_threshold=int(fg),
                    alpha_matting_background_threshold=int(bg),
                    alpha_matting_erode_size=int(erode),
                    only_mask=only_mask,
                    post_process_mask=ppm,
                    bgcolor=bgcolor,
                )

                tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
                tmp.write(out_bytes)
                tmp.flush()
                tmp.close()

                info = MODEL_INFO.get(model, ("", model))
                status = (
                    f"✅ Tamamlandı!\n"
                    f"Model: {info[0]} `{model}`\n"
                    f"Alpha Matting: {'✅' if alpha else '❌'} | "
                    f"Post-process: {'✅' if ppm else '❌'} | "
                    f"Sadece Maske: {'✅' if only_mask else '❌'}"
                )
                return tmp.name, tmp.name, status

            except Exception as e:
                import traceback
                return None, None, f"❌ Hata: {e}\n{traceback.format_exc()}"

        def process_batch(
            files,
            model, alpha, fg, bg, erode,
            only_mask, ppm, use_bgcolor, bgcolor_hex, bgcolor_alpha,
            progress=gr.Progress(),
        ):
            if not files:
                return [], [], "⚠️ Lütfen en az bir dosya yükleyin."
            try:
                session = _get_session(model)
                bgcolor = None
                if use_bgcolor and bgcolor_hex:
                    bgcolor = _hex_to_rgba(bgcolor_hex, int(bgcolor_alpha))

                out_images: list = []
                out_files:  list = []
                log_lines:  list = []
                total = len(files)

                for i, file_obj in enumerate(files):
                    progress(i / total, desc=f"İşleniyor {i+1}/{total}...")
                    fpath = file_obj.name if hasattr(file_obj, "name") else str(file_obj)
                    fname = os.path.splitext(os.path.basename(fpath))[0]

                    with open(fpath, "rb") as f:
                        data = f.read()

                    out_bytes = remove(
                        data,
                        session=session,
                        alpha_matting=alpha,
                        alpha_matting_foreground_threshold=int(fg),
                        alpha_matting_background_threshold=int(bg),
                        alpha_matting_erode_size=int(erode),
                        only_mask=only_mask,
                        post_process_mask=ppm,
                        bgcolor=bgcolor,
                    )

                    tmp = tempfile.NamedTemporaryFile(
                        suffix=".png",
                        prefix=f"{fname}_rembg_",
                        delete=False,
                    )
                    tmp.write(out_bytes)
                    tmp.flush()
                    tmp.close()

                    out_images.append((tmp.name, fname))
                    out_files.append(tmp.name)
                    log_lines.append(f"✅ [{i+1}/{total}] {fname}.png")

                progress(1.0, desc="Tamamlandı!")
                status = f"🎉 {total} resim işlendi!\n\n" + "\n".join(log_lines)
                return out_images, out_files, status

            except Exception as e:
                import traceback
                return [], [], f"❌ Hata: {e}\n{traceback.format_exc()}"

        # ── CSS ─────────────────────────────────────────────────────────
        CSS = """
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
        * { box-sizing: border-box; }

        body, .gradio-container {
            background: linear-gradient(135deg, #0d0b1e 0%, #1a1535 40%, #0f1a2e 100%) !important;
            font-family: 'Inter', system-ui, sans-serif !important;
        }

        /* Hero */
        .rembg-hero {
            background: linear-gradient(135deg,
                rgba(124,58,237,0.3), rgba(139,92,246,0.15), rgba(59,130,246,0.2));
            border: 1px solid rgba(139,92,246,0.35);
            border-radius: 20px; padding: 36px 40px;
            margin-bottom: 20px; text-align: center;
            backdrop-filter: blur(20px);
        }
        .rembg-hero h1 {
            font-size: 2.4rem; font-weight: 800;
            background: linear-gradient(135deg, #a78bfa 0%, #f472b6 50%, #60a5fa 100%);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            background-clip: text; margin: 0 0 8px;
        }
        .rembg-hero p { color: #c4b5fd; font-size: 1rem; margin: 0; }

        /* Tabs */
        .tab-nav button {
            background: rgba(255,255,255,0.06) !important;
            border: 1px solid rgba(255,255,255,0.1) !important;
            color: #a5b4fc !important; border-radius: 12px !important;
            font-weight: 600 !important; padding: 10px 20px !important;
            transition: all 0.2s ease !important;
        }
        .tab-nav button.selected {
            background: linear-gradient(135deg, #7c3aed, #6d28d9) !important;
            border-color: #8b5cf6 !important; color: white !important;
            box-shadow: 0 4px 20px rgba(124,58,237,0.4) !important;
        }
        .tab-nav button:hover:not(.selected) {
            background: rgba(139,92,246,0.18) !important;
            color: #c4b5fd !important;
        }

        /* Preset buttons */
        .preset-btn {
            background: rgba(255,255,255,0.06) !important;
            border: 1px solid rgba(255,255,255,0.12) !important;
            color: #c4b5fd !important; border-radius: 10px !important;
            font-size: 0.82rem !important; font-weight: 600 !important;
            transition: all 0.2s ease !important;
        }
        .preset-btn:hover {
            background: rgba(139,92,246,0.25) !important;
            border-color: rgba(168,85,247,0.5) !important;
            color: white !important; transform: translateY(-1px) !important;
        }

        /* Primary button */
        .gr-button-primary, button.primary {
            background: linear-gradient(135deg, #7c3aed, #6d28d9) !important;
            border: none !important; color: white !important;
            font-weight: 700 !important; border-radius: 12px !important;
            box-shadow: 0 4px 20px rgba(124,58,237,0.4) !important;
            transition: all 0.25s ease !important;
        }
        .gr-button-primary:hover, button.primary:hover {
            background: linear-gradient(135deg, #8b5cf6, #7c3aed) !important;
            box-shadow: 0 8px 28px rgba(124,58,237,0.6) !important;
            transform: translateY(-2px) !important;
        }

        /* Secondary button */
        button.secondary {
            background: rgba(255,255,255,0.08) !important;
            border: 1px solid rgba(255,255,255,0.18) !important;
            color: #c4b5fd !important; border-radius: 12px !important;
        }

        /* Labels */
        label > span, .label-wrap span {
            color: #a5b4fc !important; font-weight: 600 !important;
            font-size: 0.82rem !important; text-transform: uppercase !important;
            letter-spacing: 0.06em !important;
        }

        /* Inputs */
        input[type=text], textarea, select {
            background: rgba(255,255,255,0.07) !important;
            border: 1px solid rgba(255,255,255,0.14) !important;
            border-radius: 10px !important; color: #e2e8f0 !important;
        }

        /* Slider */
        input[type=range] { accent-color: #8b5cf6 !important; }

        /* Checkbox */
        input[type=checkbox] { accent-color: #8b5cf6 !important; }

        /* Image upload */
        .image-upload > div {
            background: rgba(139,92,246,0.06) !important;
            border: 2px dashed rgba(139,92,246,0.4) !important;
            border-radius: 14px !important; transition: all 0.25s ease !important;
        }
        .image-upload > div:hover {
            border-color: rgba(168,85,247,0.7) !important;
            background: rgba(139,92,246,0.12) !important;
        }

        /* Status textbox */
        .status-out textarea {
            background: rgba(16,185,129,0.07) !important;
            border: 1px solid rgba(16,185,129,0.25) !important;
            color: #6ee7b7 !important; font-size: 0.88rem !important;
            border-radius: 10px !important;
        }

        /* Gallery */
        .gr-gallery-item {
            border-radius: 12px !important;
            border: 1px solid rgba(255,255,255,0.1) !important;
            transition: transform 0.2s, box-shadow 0.2s !important;
        }
        .gr-gallery-item:hover {
            transform: scale(1.03) !important;
            box-shadow: 0 8px 24px rgba(0,0,0,0.4) !important;
        }

        /* Markdown */
        .gr-markdown h3 { color: #c4b5fd !important; }
        .gr-markdown p  { color: #cbd5e1 !important; }
        .gr-markdown code {
            background: rgba(139,92,246,0.2) !important;
            color: #a78bfa !important; border-radius: 6px !important;
            padding: 2px 7px !important;
        }
        .gr-markdown pre {
            background: rgba(0,0,0,0.4) !important;
            border: 1px solid rgba(255,255,255,0.1) !important;
            border-radius: 12px !important; padding: 16px !important;
        }

        /* Info card */
        .info-card {
            background: rgba(59,130,246,0.1);
            border: 1px solid rgba(59,130,246,0.3);
            border-radius: 12px; padding: 14px 18px; margin: 8px 0;
        }
        .info-card p { color: #93c5fd !important; font-size: 0.88rem; margin: 3px 0; }

        /* Footer */
        .rembg-footer {
            text-align: center; padding: 24px;
            color: rgba(167,139,250,0.5); font-size: 0.78rem;
            margin-top: 24px; border-top: 1px solid rgba(255,255,255,0.05);
        }
        .rembg-footer a { color: #8b5cf6; text-decoration: none; }
        .rembg-footer a:hover { color: #a78bfa; }

        /* Scrollbar */
        ::-webkit-scrollbar { width: 6px; height: 6px; }
        ::-webkit-scrollbar-track { background: rgba(255,255,255,0.04); border-radius: 3px; }
        ::-webkit-scrollbar-thumb { background: rgba(139,92,246,0.4); border-radius: 3px; }
        ::-webkit-scrollbar-thumb:hover { background: rgba(139,92,246,0.7); }
        """

        # Gradio 6: theme'i Blocks'a ver (mount_gradio_app için geçerli yöntem)
        _theme = gr.themes.Base(
            primary_hue="violet",
            secondary_hue="purple",
            neutral_hue="slate",
            font=gr.themes.GoogleFont("Inter"),
        )

        model_choices = get_model_choices()

        # ── Gradio Blocks ────────────────────────────────────────────────
        with gr.Blocks(
            css=CSS,
            title="🎨 Rembg — AI Arka Plan Kaldırma",
            theme=_theme,
            analytics_enabled=False,
        ) as interface:

            # ── Hero ─────────────────────────────────────────────────────
            gr.HTML("""
            <div class="rembg-hero">
                <h1>🎨 Rembg AI</h1>
                <p>Yapay Zeka ile Profesyonel Arka Plan Kaldırma &nbsp;•&nbsp;
                   15+ Model &nbsp;•&nbsp; Tekli & Toplu İşlem &nbsp;•&nbsp; REST API</p>
            </div>
            """)

            with gr.Tabs():

                # ══════════════════════════════════════════════════════════
                # SEKME 1 — Tekli İşlem
                # ══════════════════════════════════════════════════════════
                with gr.Tab("🖼️ Tekli İşlem"):
                    with gr.Row(equal_height=False):

                        # Sol: Ayarlar
                        with gr.Column(scale=1, min_width=300):
                            gr.Markdown("### 🎛️ Hız / Kalite Presetleri")
                            with gr.Row():
                                preset_btns = []
                                for pname in PRESETS:
                                    btn = gr.Button(pname, elem_classes=["preset-btn"], size="sm")
                                    preset_btns.append((pname, btn))

                            gr.Markdown("---")
                            gr.Markdown("### ⚙️ Model & Parametreler")

                            s_input = gr.Image(
                                type="filepath",
                                label="Resim Yükle",
                                elem_classes=["image-upload"],
                            )
                            s_model = gr.Dropdown(
                                choices=model_choices,
                                value="u2net",
                                label="AI Modeli",
                                info="Kullanım alanına göre seçin",
                            )

                            with gr.Accordion("🔬 Alpha Matting (Kenarlık İyileştirme)", open=False):
                                s_alpha = gr.Checkbox(value=False, label="Alpha Matting Aktif")
                                s_fg    = gr.Slider(0, 255, value=240, step=1, label="Ön Plan Eşiği")
                                s_bg    = gr.Slider(0, 255, value=10,  step=1, label="Arka Plan Eşiği")
                                s_erode = gr.Slider(0, 50,  value=10,  step=1, label="Erozyon Boyutu")

                            with gr.Accordion("🎨 Çıktı Seçenekleri", open=False):
                                s_only_mask = gr.Checkbox(value=False, label="Sadece Maske Çıktısı")
                                s_ppm       = gr.Checkbox(value=True,  label="Maske Post-Process")

                            with gr.Accordion("🖌️ Arka Plan Rengi", open=False):
                                s_use_bg  = gr.Checkbox(value=False, label="Arka Plan Rengi Ekle")
                                s_bgcol   = gr.ColorPicker(value="#ffffff", label="Renk")
                                s_bgalpha = gr.Slider(0, 255, value=255, step=1, label="Saydamlık (Alpha)")

                            with gr.Row():
                                s_btn   = gr.Button("🚀 Arka Planı Kaldır", variant="primary", size="lg")
                                s_clear = gr.Button("🗑️ Temizle", variant="secondary")

                            s_status = gr.Textbox(
                                label="Durum", interactive=False, lines=3,
                                elem_classes=["status-out"],
                            )

                        # Sağ: Sonuç
                        with gr.Column(scale=2):
                            gr.Markdown("### 📊 Önizleme & İndirme")
                            with gr.Row():
                                s_original = gr.Image(
                                    type="filepath", label="📥 Orijinal",
                                    interactive=False, height=380,
                                )
                                s_result = gr.Image(
                                    type="filepath", label="✨ İşlenmiş (PNG)",
                                    interactive=False, height=380,
                                )
                            s_download = gr.File(
                                label="📥 PNG İndir",
                            )

                    # Preset bağlantıları
                    preset_outputs = [s_model, s_alpha, s_fg, s_bg, s_erode, s_ppm, s_status]
                    for pname, pbtn in preset_btns:
                        pbtn.click(
                            fn=lambda p=pname: apply_preset(p),
                            outputs=preset_outputs,
                        )

                    # İşlem
                    s_btn.click(
                        fn=process_single,
                        inputs=[
                            s_input, s_model, s_alpha, s_fg, s_bg, s_erode,
                            s_only_mask, s_ppm, s_use_bg, s_bgcol, s_bgalpha,
                        ],
                        outputs=[s_result, s_download, s_status],
                        concurrency_limit=3,
                    )

                    # Orijinali yan yana göster
                    s_input.change(fn=lambda x: x, inputs=s_input, outputs=s_original)

                    # Temizle
                    s_clear.click(
                        fn=lambda: (None, None, None, None, ""),
                        outputs=[s_input, s_original, s_result, s_download, s_status],
                    )

                # ══════════════════════════════════════════════════════════
                # SEKME 2 — Toplu İşlem
                # ══════════════════════════════════════════════════════════
                with gr.Tab("📦 Toplu İşlem (Batch)"):
                    with gr.Row(equal_height=False):

                        # Sol: Ayarlar
                        with gr.Column(scale=1, min_width=300):
                            gr.Markdown("### ⚙️ Toplu Ayarlar")

                            with gr.Row():
                                batch_preset_btns = []
                                for pname in PRESETS:
                                    btn = gr.Button(pname, elem_classes=["preset-btn"], size="sm")
                                    batch_preset_btns.append((pname, btn))

                            gr.Markdown("---")

                            b_files = gr.Files(
                                label="Resimler Yükle (Çoklu Seçim)",
                                file_types=["image"],
                                file_count="multiple",
                            )
                            b_model = gr.Dropdown(
                                choices=model_choices,
                                value="u2net",
                                label="AI Modeli",
                            )

                            with gr.Accordion("🔬 Alpha Matting", open=False):
                                b_alpha = gr.Checkbox(value=False, label="Alpha Matting Aktif")
                                b_fg    = gr.Slider(0, 255, value=240, step=1, label="Ön Plan Eşiği")
                                b_bg    = gr.Slider(0, 255, value=10,  step=1, label="Arka Plan Eşiği")
                                b_erode = gr.Slider(0, 50,  value=10,  step=1, label="Erozyon Boyutu")

                            with gr.Accordion("🎨 Çıktı Seçenekleri", open=False):
                                b_only_mask = gr.Checkbox(value=False, label="Sadece Maske")
                                b_ppm       = gr.Checkbox(value=True,  label="Maske Post-Process")

                            with gr.Accordion("🖌️ Arka Plan Rengi", open=False):
                                b_use_bg  = gr.Checkbox(value=False, label="Arka Plan Rengi Ekle")
                                b_bgcol   = gr.ColorPicker(value="#ffffff", label="Renk")
                                b_bgalpha = gr.Slider(0, 255, value=255, step=1, label="Saydamlık")

                            b_btn    = gr.Button("🚀 Tümünü İşle", variant="primary", size="lg")
                            b_status = gr.Textbox(
                                label="Durum", interactive=False, lines=5,
                                elem_classes=["status-out"],
                            )

                        # Sağ: Sonuçlar
                        with gr.Column(scale=2):
                            gr.Markdown("### 📊 Sonuçlar — Tek Tek İndir")
                            gr.HTML("""
                            <div class="info-card">
                              <p>🖼️ Her işlenmiş resim ayrı ayrı indirilebilir.</p>
                              <p>📋 Önizleme galerisine tıklayarak büyütebilirsiniz.</p>
                            </div>
                            """)
                            b_gallery = gr.Gallery(
                                label="İşlenmiş Resimler (Önizleme)",
                                columns=3,
                                height=300,
                                object_fit="contain",
                            )
                            b_outfiles = gr.Files(
                                label="📥 İndirme Listesi (Tek Tek İndir)",
                                file_count="multiple",
                                interactive=False,
                            )

                    # Batch preset bağlantıları
                    b_preset_outputs = [b_model, b_alpha, b_fg, b_bg, b_erode, b_ppm, b_status]
                    for pname, pbtn in batch_preset_btns:
                        pbtn.click(
                            fn=lambda p=pname: apply_preset(p),
                            outputs=b_preset_outputs,
                        )

                    b_btn.click(
                        fn=process_batch,
                        inputs=[
                            b_files, b_model, b_alpha, b_fg, b_bg, b_erode,
                            b_only_mask, b_ppm, b_use_bg, b_bgcol, b_bgalpha,
                        ],
                        outputs=[b_gallery, b_outfiles, b_status],
                        concurrency_limit=1,
                    )

                # ══════════════════════════════════════════════════════════
                # SEKME 3 — Model Rehberi
                # ══════════════════════════════════════════════════════════
                with gr.Tab("🧠 Model Rehberi"):
                    gr.Markdown("## 🧠 Model Karşılaştırma Tablosu")
                    _rows = [
                        ("u2net",               "🎯 Genel",      "Her şey için",                 "🟡 Orta",      "⭐⭐⭐⭐"),
                        ("u2netp",              "⚡ Hızlı",       "Hafif / hızlı işlem",           "🟢 Hızlı",    "⭐⭐⭐"),
                        ("u2net_human_seg",     "👤 İnsan",      "Portre, kişi fotoğrafı",         "🟡 Orta",      "⭐⭐⭐⭐"),
                        ("u2net_cloth_seg",     "👗 Kıyafet",    "E-ticaret, tekstil",             "🟡 Orta",      "⭐⭐⭐⭐"),
                        ("silueta",             "🖤 Siluet",     "Sanatsal, siluet",               "🟢 Hızlı",    "⭐⭐⭐"),
                        ("birefnet-general",    "✨ BiRefNet",   "Profesyonel genel",              "🔴 Yavaş",    "⭐⭐⭐⭐⭐"),
                        ("birefnet-general-lite","✨ Lite",       "Hızlı yüksek kalite",           "🟡 Orta",      "⭐⭐⭐⭐"),
                        ("birefnet-portrait",   "🧑 Portre",     "Headshot, profil fotoğrafı",     "🔴 Yavaş",    "⭐⭐⭐⭐⭐"),
                        ("birefnet-dis",        "🔬 Detaylı",    "Karmaşık arka planlar",          "🔴 Yavaş",    "⭐⭐⭐⭐⭐"),
                        ("birefnet-hrsod",      "📸 Yüksek Çöz.","Büyük resimler",               "🔴 Yavaş",    "⭐⭐⭐⭐⭐"),
                        ("birefnet-cod",        "🦎 Kamuflaj",   "Gizli/kamuflajlı nesneler",     "🔴 Yavaş",    "⭐⭐⭐⭐"),
                        ("birefnet-massive",    "💪 Massive",    "Maksimum kalite",                "🔴 Çok Yavaş","⭐⭐⭐⭐⭐"),
                        ("dis-general-use",     "🌐 DIS",        "İnce kenarlıklar (saç, kürk)",  "🟡 Orta",      "⭐⭐⭐⭐⭐"),
                        ("dis-anime",           "🎌 Anime",      "Anime, çizgi karakter",          "🟡 Orta",      "⭐⭐⭐⭐⭐"),
                        ("bria-rmbg",           "🏢 Ticari",     "Profesyonel ürün fotoğrafı",     "🟡 Orta",      "⭐⭐⭐⭐⭐"),
                    ]
                    table_html = """
                    <div style="overflow-x:auto;margin-top:16px;">
                    <table style="width:100%;border-collapse:collapse;font-family:Inter,sans-serif;font-size:0.88rem;">
                    <thead><tr style="background:linear-gradient(135deg,rgba(124,58,237,0.35),rgba(109,40,217,0.25));">
                    """ + "".join(
                        f'<th style="padding:14px 16px;text-align:left;color:#e2e8f0;'
                        f'border-bottom:2px solid rgba(139,92,246,0.4);">{h}</th>'
                        for h in ["Model", "Tür", "Kullanım Alanı", "Hız", "Kalite"]
                    ) + """</tr></thead><tbody>""" + "".join(
                        f'<tr style="background:{"rgba(255,255,255,0.04)" if i%2==0 else "rgba(255,255,255,0.02)"};'
                        f'border-bottom:1px solid rgba(255,255,255,0.05);">'
                        f'<td style="padding:11px 16px;color:#a78bfa;font-weight:600;font-family:monospace;">{r[0]}</td>'
                        f'<td style="padding:11px 16px;color:#e2e8f0;">{r[1]}</td>'
                        f'<td style="padding:11px 16px;color:#cbd5e1;">{r[2]}</td>'
                        f'<td style="padding:11px 16px;">{r[3]}</td>'
                        f'<td style="padding:11px 16px;">{r[4]}</td></tr>'
                        for i, r in enumerate(_rows)
                    ) + "</tbody></table></div>"
                    gr.HTML(table_html)
                    gr.HTML("""
                    <div class="info-card" style="margin-top:20px;">
                      <p>💡 <b>Hız öncelikliyse:</b> u2netp veya silueta</p>
                      <p>💡 <b>Kalite öncelikliyse:</b> birefnet-general veya birefnet-portrait</p>
                      <p>💡 <b>İnsan/portre:</b> u2net_human_seg veya birefnet-portrait</p>
                      <p>💡 <b>Saç/kürk detayı:</b> dis-general-use (Alpha Matting ile)</p>
                      <p>💡 <b>Anime/çizgi:</b> dis-anime</p>
                    </div>
                    """)

                # ══════════════════════════════════════════════════════════
                # SEKME 4 — REST API
                # ══════════════════════════════════════════════════════════
                with gr.Tab("🔌 REST API"):
                    gr.Markdown(f"""
## 🔌 REST API Kullanımı

**Swagger UI (interaktif):** [http://localhost:{port}/api](http://localhost:{port}/api)

---

### 📤 POST — Dosya Yükle
```bash
curl -X POST "http://localhost:{port}/api/remove" \\
  -F "file=@resim.jpg" \\
  --output sonuc.png
```

### 🌐 GET — URL'den İşle
```bash
curl "http://localhost:{port}/api/remove?url=https://example.com/photo.jpg" \\
  --output sonuc.png
```

### ⚙️ Parametreler
| Parametre | Tür | Açıklama | Varsayılan |
|-----------|-----|----------|-----------|
| `model` | string | Model adı | `u2net` |
| `a` | bool | Alpha matting | `false` |
| `af` | int 0–255 | Ön plan eşiği | `240` |
| `ab` | int 0–255 | Arka plan eşiği | `10` |
| `ae` | int | Erozyon boyutu | `10` |
| `om` | bool | Sadece maske | `false` |
| `ppm` | bool | Post-process mask | `false` |
| `bgc` | string | Arka plan rengi `R,G,B,A` | — |

### 🐍 Python Örneği
```python
import requests

with open("resim.jpg", "rb") as f:
    r = requests.post(
        "http://localhost:{port}/api/remove",
        files={{"file": f}},
        params={{"model": "birefnet-portrait", "a": True}}
    )

with open("sonuc.png", "wb") as out:
    out.write(r.content)
```

### 🐚 Bash — Toplu İşlem
```bash
for img in *.jpg; do
  curl -s -X POST "http://localhost:{port}/api/remove" \\
    -F "file=@$img" \\
    -o "${{img%.jpg}}_rembg.png"
  echo "✅ $img işlendi"
done
```
                    """)

                # ══════════════════════════════════════════════════════════
                # SEKME 5 — Podman Kılavuzu
                # ══════════════════════════════════════════════════════════
                with gr.Tab("🐳 Podman Kılavuzu"):
                    gr.Markdown(f"""
## 🐳 Podman ile Çalıştırma

### 📦 Hızlı Başlangıç
```bash
cd /mnt/local/projects/rembg

# İlk kez (5-10 dk — bağımlılıklar + u2net modeli indirilir)
./podman-run.sh

# Kod güncellemesi sonrası (rebuild YOK, 5 saniye!)
./podman-run.sh --update
```

### 🔧 Yönetim Komutları
```bash
./podman-run.sh            # Build + başlat
./podman-run.sh --update   # ⚡ Kodu güncelle (rebuild yok)
./podman-run.sh --stop     # Durdur
./podman-run.sh --restart  # Yeniden başlat
./podman-run.sh --logs     # Canlı log takibi
./podman-run.sh --status   # Durum + son loglar
./podman-run.sh --shell    # Container içine gir
./podman-run.sh --rebuild  # Tam yeniden build
./podman-run.sh --clean    # Her şeyi sil
```

### 🖥️ Podman Desktop Kurulumu
```bash
# Flatpak ile
flatpak install flathub io.podman_desktop.PodmanDesktop
```

### 🌐 Port Değiştirme
```bash
REMBG_PORT=8080 ./podman-run.sh
```

### 💾 Model Cache (Kalıcı)
```bash
# Model cache nerede?
podman volume inspect rembg-models

# Disk kullanımı
podman system df
```
                    """)

            # Footer
            gr.HTML(f"""
            <div class="rembg-footer">
                🎨 Rembg v{__version__} &nbsp;•&nbsp;
                FastAPI + Gradio {gr.__version__} &nbsp;•&nbsp;
                CPU Backend &nbsp;•&nbsp;
                <a href="https://github.com/danielgatis/rembg" target="_blank">GitHub</a>
                &nbsp;•&nbsp;
                <a href="/api" target="_blank">Swagger API</a>
            </div>
            """)

        fastapi_app = gr.mount_gradio_app(fastapi_app, interface, path="/")
        return fastapi_app

    # -------------------------------------------------------------------
    print(f"\n🔌 API : http://{'localhost' if host == '0.0.0.0' else host}:{port}/api")
    if not no_ui:
        print(f"🎨 UI  : http://{'localhost' if host == '0.0.0.0' else host}:{port}\n")

    uvicorn.run(
        app if no_ui else gr_app(app),
        host=host,
        port=port,
        log_level=log_level,
    )
