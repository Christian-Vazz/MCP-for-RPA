from django.urls import path

from . import api, views

urlpatterns = [
    path("", views.chat, name="chat"),
    path("api/runs/", api.run_create),
    path("api/runs/<int:run_id>/events/", api.run_events),
    path("api/runs/<int:run_id>/cancel/", api.run_cancel),
    path("api/approvals/<int:approval_id>/", api.approval_decide),
    path("api/conversations/", api.conversations),
    path("api/conversations/<int:pk>/", api.conversation_detail),
    path("api/messages/<int:pk>/feedback/", api.message_feedback),
    path("api/tools/", api.tools_list),
    path("api/tools/run/", api.tool_run),
    path("api/memories/", api.memories),
    path("api/memories/<int:pk>/", api.memory_detail),
]
