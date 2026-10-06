from django.urls import path

from apps.core.placeholders import placeholder

app_name = "patients"

urlpatterns = [
    path("", placeholder, name="list"),
    path("search.json", placeholder, name="search_json"),
    path("new/", placeholder, name="create"),
    path("import/", placeholder, name="import"),
    path("import/template.csv", placeholder, name="import_template"),
    path("<int:pk>/", placeholder, name="detail"),
    path("<int:pk>/edit/", placeholder, name="update"),
    path("<int:pk>/archive/", placeholder, name="archive"),
]
