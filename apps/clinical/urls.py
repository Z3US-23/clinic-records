from django.urls import path

from . import views

app_name = "clinical"

urlpatterns = [
    path("visits/", views.VisitListView.as_view(), name="visit_list"),
    path("patients/<int:patient_pk>/visits/new/", views.visit_create, name="visit_create"),
    path("visits/<int:pk>/", views.VisitDetailView.as_view(), name="visit_detail"),
    path("visits/<int:pk>/edit/", views.visit_update, name="visit_update"),
    path("visits/<int:pk>/prescription/", views.prescription_print, name="prescription_print"),
    path("patients/<int:patient_pk>/labs/new/", views.lab_create, name="lab_create"),
    path("labs/<int:pk>/edit/", views.lab_update, name="lab_update"),
    path("labs/<int:pk>/file/", views.lab_file, name="lab_file"),
    path("labs/<int:pk>/delete/", views.lab_delete, name="lab_delete"),
]
