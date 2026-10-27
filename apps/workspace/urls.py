from django.urls import path

from . import views

urlpatterns = [
    path("api/files/", views.files_list),
    path("api/files/upload/", views.files_upload),
    path("api/files/read/", views.files_read),
    path("api/files/download/", views.files_download),
    path("api/files/delete/", views.files_delete),
    path("api/files/undo/", views.files_undo),
]
