from django.contrib import admin

from .models import Document, Memory, Retrieval

admin.site.register(Document)
admin.site.register(Memory)
admin.site.register(Retrieval)
