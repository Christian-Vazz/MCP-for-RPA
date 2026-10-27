import json
import re

from django.core.management.base import BaseCommand

from apps.agent.models import TrainingSample

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"\+?\d[\d\s().-]{8,}\d")
_CPF = re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b")


def scrub(text: str) -> str:
    for rx, tag in ((_EMAIL, "[email]"), (_CPF, "[cpf]"), (_PHONE, "[telefone]")):
        text = rx.sub(tag, text)
    return text


class Command(BaseCommand):
    help = "Exporta amostras (somente de usuários que consentiram) em JSONL, com PII básica removida."

    def add_arguments(self, parser):
        parser.add_argument("--out", default="training_data.jsonl")
        parser.add_argument("--include-unrated", action="store_true", help="Inclui amostras sem avaliação.")

    def handle(self, *args, **opts):
        qs = TrainingSample.objects.filter(user__profile__allow_learning=True)
        qs = qs.filter(rating__gte=0) if opts["include_unrated"] else qs.filter(rating=1)
        n = 0
        with open(opts["out"], "w", encoding="utf-8") as fh:
            for s in qs.iterator():
                fh.write(json.dumps({"messages": [
                    {"role": "user", "content": scrub(s.prompt)},
                    {"role": "assistant", "content": scrub(s.answer)}],
                    "metadata": {"tools": s.tools, "rating": s.rating}}, ensure_ascii=False) + "\n")
                n += 1
        self.stdout.write(self.style.SUCCESS(f"{n} amostras exportadas para {opts['out']}"))
