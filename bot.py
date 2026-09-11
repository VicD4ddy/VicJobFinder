# -*- coding: utf-8 -*-
"""
Job Monitor Bot - Monitoreo y Notificación Autónomo de Vacantes Remotas ($0/mes)
Ejecutado periódicamente vía GitHub Actions y notificado a través de Telegram.
"""

import os
import sys
import json
import time
import logging
import hashlib
import xml.etree.ElementTree as ET
from typing import List, Dict, Any, Optional
from datetime import datetime
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

# Cargar variables de entorno locales si existen
load_dotenv()

# Configuración de logging estructurado
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("JobMonitorBot")

STATE_FILE = os.path.join(os.path.dirname(__file__), "vistos.json")
MAX_STORED_JOBS = 1000  # Límite circular para evitar crecimiento indefinido de vistos.json
MAX_ALERTS_PER_RUN = 10  # Límite de mensajes por ciclo para evitar flooding en Telegram

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/json,*/*;q=0.8",
}


class StateManager:
    """Gestiona la persistencia y consulta de identificadores de ofertas ya vistas."""

    def __init__(self, filepath: str = STATE_FILE, max_size: int = MAX_STORED_JOBS):
        self.filepath = filepath
        self.max_size = max_size
        self.seen_ids: set = set()
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self.filepath):
            logger.info(f"Archivo de estado {self.filepath} no encontrado. Creando nuevo registro.")
            self.seen_ids = set()
            self._save()
            return

        try:
            with open(self.filepath, "r", encoding="utf-8-sig") as f:
                content = f.read().strip()
                if not content:
                    self.seen_ids = set()
                    return
                data = json.loads(content)
                if isinstance(data, list):
                    self.seen_ids = set(data)
                elif isinstance(data, dict) and "seen_ids" in data:
                    self.seen_ids = set(data["seen_ids"])
                else:
                    self.seen_ids = set()
            logger.info(f"Estado cargado: {len(self.seen_ids)} vacantes previas en registro.")
        except Exception as e:
            logger.warning(f"Aviso al cargar estado ({e}). Inicializando registro en blanco.")
            self.seen_ids = set()

    def _save(self) -> None:
        try:
            list_ids = list(self.seen_ids)
            # Conservar los últimos N elementos para buffer circular
            if len(list_ids) > self.max_size:
                list_ids = list_ids[-self.max_size:]
                self.seen_ids = set(list_ids)

            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(list_ids, f, indent=2, ensure_ascii=False)
            logger.info(f"Estado actualizado exitosamente en {self.filepath}.")
        except Exception as e:
            logger.error(f"Fallo al escribir en {self.filepath}: {e}")

    def is_seen(self, job_id: str) -> bool:
        return job_id in self.seen_ids

    def mark_seen(self, job_id: str) -> None:
        self.seen_ids.add(job_id)

    def persist(self) -> None:
        self._save()


class TelegramNotifier:
    """Maneja la construcción HTML y transmisión de alertas a Telegram Bot API."""

    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None):
        self.token = token or os.getenv("TELEGRAM_TOKEN")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID")
        self.enabled = bool(self.token and self.chat_id)

        if not self.enabled:
            logger.warning(
                "Credenciales no configuradas (TELEGRAM_TOKEN / TELEGRAM_CHAT_ID). "
                "Operando en MODO DRY-RUN (simulación local)."
            )

    @staticmethod
    def escape_html(text: str) -> str:
        """Sanitiza cadenas de texto para evitar inconsistencias de formato HTML en Telegram."""
        if not text:
            return ""
        return (
            str(text)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    def send_job_alert(self, job: Dict[str, Any]) -> bool:
        title = self.escape_html(job.get("title", "Sin título"))
        company = self.escape_html(job.get("company", "Empresa Confidencial"))
        location = self.escape_html(job.get("location", "Remoto (Global)"))
        source = self.escape_html(job.get("source", "Web"))
        url = job.get("url", "")
        tags_raw = job.get("tags", [])
        tags = self.escape_html(", ".join(tags_raw) if tags_raw else "Software, Tech")

        message = (
            "🚀 <b>Nueva Vacante Remota Detectada</b>\n\n"
            f"💼 <b>Puesto:</b> {title}\n"
            f"🏢 <b>Empresa:</b> {company}\n"
            f"📍 <b>Ubicación:</b> {location}\n"
            f"🏷️ <b>Etiquetas:</b> {tags}\n"
            f"🌐 <b>Fuente:</b> {source}\n\n"
            f'<a href="{url}"><b>Postularse / Ver Oferta Completa</b></a>'
        )

        if not self.enabled:
            logger.info(f"[DRY-RUN SIMULACIÓN TELEGRAM]:\n{message}\n" + "-" * 40)
            return True

        api_url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": False
        }

        try:
            response = requests.post(api_url, json=payload, timeout=15)
            if response.status_code == 200:
                logger.info(f"Notificación enviada: {job.get('title')} ({job.get('company')})")
                return True
            else:
                logger.error(f"Fallo Telegram HTTP {response.status_code}: {response.text}")
                return False
        except Exception as e:
            logger.error(f"Error de conexión con Telegram API: {e}")
            return False


class JobScraper:
    """Extrae y estandariza ofertas remotas desde fuentes públicas abiertas."""

    @staticmethod
    def _generate_id(seed: str) -> str:
        return hashlib.md5(seed.strip().lower().encode("utf-8")).hexdigest()

    @staticmethod
    def clean_html(raw_html: str) -> str:
        """Limpia etiquetas HTML usando BeautifulSoup para obtener texto plano."""
        if not raw_html:
            return ""
        soup = BeautifulSoup(raw_html, "html.parser")
        return soup.get_text(separator=" ", strip=True)

    def scrape_we_work_remotely(self) -> List[Dict[str, Any]]:
        """Extrae vacantes de programación de We Work Remotely vía feed RSS/XML."""
        url = "https://weworkremotely.com/categories/remote-programming-jobs.rss"
        jobs: List[Dict[str, Any]] = []
        logger.info(f"Extrayendo vacantes de We Work Remotely: {url}")

        try:
            response = requests.get(url, headers=HEADERS, timeout=15)
            if response.status_code != 200:
                logger.warning(f"We Work Remotely respondió HTTP {response.status_code}")
                return jobs

            root = ET.fromstring(response.content)
            items = root.findall(".//item")

            for item in items:
                title_elem = item.find("title")
                link_elem = item.find("link")
                guid_elem = item.find("guid")
                pub_elem = item.find("pubDate")
                desc_elem = item.find("description")

                full_title = title_elem.text.strip() if (title_elem is not None and title_elem.text) else ""
                job_url = link_elem.text.strip() if (link_elem is not None and link_elem.text) else ""
                guid = guid_elem.text.strip() if (guid_elem is not None and guid_elem.text) else job_url

                if not full_title or not job_url:
                    continue

                company = "We Work Remotely"
                title = full_title
                if ":" in full_title:
                    parts = full_title.split(":", 1)
                    company = parts[0].strip()
                    title = parts[1].strip()

                unique_id = self._generate_id(f"wwr_{guid}")
                clean_desc = self.clean_html(desc_elem.text) if (desc_elem is not None and desc_elem.text) else ""

                jobs.append({
                    "id": unique_id,
                    "title": title,
                    "company": company,
                    "url": job_url,
                    "location": "Remoto",
                    "source": "We Work Remotely",
                    "tags": ["Programming", "Software"],
                    "description": clean_desc[:200],
                    "date": pub_elem.text.strip() if (pub_elem is not None and pub_elem.text) else ""
                })

            logger.info(f"We Work Remotely: {len(jobs)} vacantes procesadas.")
        except Exception as e:
            logger.error(f"Error procesando We Work Remotely: {e}")

        return jobs

    def scrape_remotive(self) -> List[Dict[str, Any]]:
        """Extrae vacantes de desarrollo de software desde la API pública de Remotive."""
        url = "https://remotive.com/api/remote-jobs?category=software-dev&limit=25"
        jobs: List[Dict[str, Any]] = []
        logger.info(f"Extrayendo vacantes de Remotive: {url}")

        try:
            response = requests.get(url, headers=HEADERS, timeout=15)
            if response.status_code != 200:
                logger.warning(f"Remotive respondió HTTP {response.status_code}")
                return jobs

            data = response.json()
            raw_jobs = data.get("jobs", [])

            for item in raw_jobs:
                job_id = str(item.get("id", ""))
                title = item.get("title", "").strip()
                company = item.get("company_name", "").strip()
                job_url = item.get("url", "").strip()
                location = item.get("candidate_required_location", "Remoto").strip()
                tags = item.get("tags", [])
                raw_desc = item.get("description", "")
                clean_desc = self.clean_html(raw_desc)[:200] if raw_desc else ""

                if not title or not job_url:
                    continue

                unique_id = self._generate_id(f"remotive_{job_id or job_url}")

                jobs.append({
                    "id": unique_id,
                    "title": title,
                    "company": company or "Remotive Partner",
                    "url": job_url,
                    "location": location or "Remoto",
                    "source": "Remotive",
                    "tags": tags[:4] if tags else ["Software Dev"],
                    "description": clean_desc,
                    "date": item.get("publication_date", "")
                })

            logger.info(f"Remotive: {len(jobs)} vacantes procesadas.")
        except Exception as e:
            logger.error(f"Error procesando Remotive: {e}")

        return jobs

    def fetch_all_jobs(self) -> List[Dict[str, Any]]:
        """Consolida vacantes de todas las fuentes configuradas."""
        all_jobs: List[Dict[str, Any]] = []
        all_jobs.extend(self.scrape_we_work_remotely())
        all_jobs.extend(self.scrape_remotive())
        return all_jobs


def run_bot() -> None:
    logger.info("=== Iniciando ciclo de monitoreo de vacantes remotas ===")
    state_manager = StateManager()
    notifier = TelegramNotifier()
    scraper = JobScraper()

    # 1. Extracción de vacantes
    all_jobs = scraper.fetch_all_jobs()
    logger.info(f"Total de ofertas recolectadas: {len(all_jobs)}")

    # 2. Filtrado contra estado histórico
    new_jobs = [j for j in all_jobs if not state_manager.is_seen(j["id"])]
    logger.info(f"Nuevas vacantes no notificadas: {len(new_jobs)}")

    if not new_jobs:
        logger.info("No hay nuevas vacantes en este ciclo. Finalizando.")
        return

    # 3. Notificación con rate-limiting preventivo
    alert_count = 0
    for job in new_jobs:
        if alert_count < MAX_ALERTS_PER_RUN:
            success = notifier.send_job_alert(job)
            if success:
                alert_count += 1
                time.sleep(1.0)  # Throttling preventivo Telegram API

        # Marcamos como visto para evitar reenvíos futuros
        state_manager.mark_seen(job["id"])

    # 4. Persistir estado en disco
    state_manager.persist()
    logger.info(f"Ciclo completado. {alert_count} vacantes notificadas. Estado persistido.")


if __name__ == "__main__":
    run_bot()
