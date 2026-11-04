"""``python manage.py expense_ai_check`` - one round trip to the model, from where the code runs.

Moved from Flask's ``flask expense-ai check`` (2026-11-04). It sends a tiny generated image, never a
customer receipt, and prints the route, the model, the latency and the token usage, so "is this
configured correctly" is a fact rather than a guess. Exits 1 when the model did not answer.
"""

from __future__ import annotations

import io

from django.core.management.base import BaseCommand, CommandError
from minty_shared import ai
from PIL import Image

from petty_cash.services import receipt_reader as rr

_CONTEXT = {
    "entity": {"id": "check", "name": "Connectivity check", "country": "HK", "currency": "HKD"},
    "accounts": [{"id": "acc-1", "code": "5100", "name": "Courier Expense"}],
    "contacts": [{"id": "con-1", "name": "SF Express"}],
}


class Command(BaseCommand):
    help = "One round-trip call to the receipt reader's model (a generated image, no customer data)."

    def handle(self, *args, **options):
        config = rr.CONFIG
        self.stdout.write(f"enabled          : {config.enabled()}")
        self.stdout.write(f"route            : {'Vertex AI' if ai.uses_vertex() else 'direct Gemini API'}")
        self.stdout.write(f"location         : {ai.location()}")
        self.stdout.write(f"model            : {config.model_id()}")
        self.stdout.write(f"thinking_level   : {config.thinking_level()}")
        if not ai.uses_vertex():
            self.stdout.write(self.style.WARNING(
                "WARNING: the direct Gemini API has no regional endpoint and no residency commitment. "
                "On the free tier Google may use submitted content to develop its products and human "
                "reviewers may read it. Use non-customer receipts only."
            ))

        buffer = io.BytesIO()
        Image.new("RGB", (2, 2), (255, 255, 255)).save(buffer, format="PNG")
        result = ai.read(buffer.getvalue(), "image/png", config=config, instruction=rr.INSTRUCTION,
                         prefix_text=rr.prefix_text(_CONTEXT), closing_text=rr.CLOSING,
                         reply_model=rr.ExpenseSuggestion,
                         validate=lambda parsed: rr.validate(parsed, _CONTEXT))

        audit = result.audit
        self.stdout.write(f"latency_ms       : {audit.get('latency_ms')}")
        self.stdout.write(f"request id       : {audit.get('provider_request_id')}")
        self.stdout.write(f"tokens in / out  : {audit.get('input_tokens')} / {audit.get('output_tokens')}")
        # Zero across repeated same-entity calls: the prefix is under the cache minimum or unstable.
        self.stdout.write(f"cached tokens    : {audit.get('cached_tokens')}")
        self.stdout.write(f"estimated cost   : {audit.get('estimated_cost')}")
        if result.reason not in (None, ai.REASON_NO_USABLE_FIELD):
            raise CommandError(f"FAILED: {result.reason}")
        self.stdout.write(self.style.SUCCESS(
            "Round trip OK - the model answered and the reply passed the schema. "
            "(A blank image yields no usable field, as expected.)"
        ))
