from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("apps.accounts.urls")),
    path("", include("apps.agent.urls")),
    path("", include("apps.workspace.urls")),
    path("", include("apps.dashboard.urls")),
]
