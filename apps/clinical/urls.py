from django.urls import path

from apps.core.placeholders import placeholder

app_name = "clinical"

urlpatterns = [
    path("visits/", placeholder, name="visit_list"),
    path("patients/<int:patient_pk>/visits/new/", placeholder, name="visit_create"),
    path("visits/<int:pk>/", placeholder, name="visit_detail"),
    path("visits/<int:pk>/edit/", placeholder, name="visit_update"),
    path("visits/<int:pk>/prescription/", placeholder, name="prescription_print"),
    path("patients/<int:patient_pk>/labs/new/", placeholder, name="lab_create"),
    path("labs/<int:pk>/file/", placeholder, name="lab_file"),
    path("labs/<int:pk>/delete/", placeholder, name="lab_delete"),
]
