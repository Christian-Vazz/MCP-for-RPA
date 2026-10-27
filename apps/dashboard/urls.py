from django.urls import path

from . import views

urlpatterns = [
    path("dashboard/", views.dashboard, name="dashboard"),
    path("api/dashboard/", views.dashboard_data),
    path("api/dashboard/rag-health/", views.rag_health),
    path("api/dashboard/rag-test/", views.rag_test),
]
