from django.urls import path

from . import views

app_name = "patients"

urlpatterns = [
    path("", views.PatientListView.as_view(), name="list"),
    path("search.json", views.search_json, name="search_json"),
    path("new/", views.PatientCreateView.as_view(), name="create"),
    path("import/", views.import_patients, name="import"),
    path("import/template.csv", views.import_template, name="import_template"),
    path("<int:pk>/", views.PatientDetailView.as_view(), name="detail"),
    path("<int:pk>/edit/", views.PatientUpdateView.as_view(), name="update"),
    path("<int:pk>/archive/", views.archive, name="archive"),
]
