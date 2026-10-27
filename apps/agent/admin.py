from django.contrib import admin

from .models import ActionLog, Conversation, Run, TrainingSample

admin.site.register(Conversation)
admin.site.register(Run)
admin.site.register(ActionLog)
admin.site.register(TrainingSample)
