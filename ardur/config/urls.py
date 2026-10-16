from django.contrib import admin
from django.urls import path, include
from ardur.apps.core import views as core, api
from ardur.apps.financiera import views as fin
from ardur.apps.ordenes import views as orders
from ardur.apps.expedientes import views as cases
from ardur.apps.notificaciones import views as notif
from ardur.apps.mapas import views as maps
from ardur.apps.auditoria import views as audit
from ardur.apps.documentos import views as docs

urlpatterns = [
    path('', core.home, name='home'),
    path('health/', core.health),
    path('admin/', admin.site.urls),
    path('cuentas/', include('django.contrib.auth.urls')),
    path('financiera/', fin.dashboard, name='fin-dashboard'),
    path('financiera/carteras/', fin.importaciones, name='fin-carteras'),
    path('financiera/importar/', fin.nueva, name='fin-nueva'),
    path('financiera/depuracion/', fin.ultima_depuracion, name='fin-actual'),
    path('financiera/excepciones/', fin.ultima_depuracion, {'excepciones': True}, name='fin-excepciones'),
    path('financiera/carteras/<uuid:pk>/', fin.depuracion, name='fin-depuracion'),
    path('financiera/carteras/<uuid:pk>/estado/', fin.estado, name='fin-estado'),
    path('financiera/carteras/<uuid:pk>/aprobar/', fin.aprobar_view, name='fin-aprobar'),
    path('financiera/carteras/<uuid:pk>/reintentar/', fin.reintentar, name='fin-reintentar'),
    path('financiera/registros/<uuid:pk>/', fin.revision, name='fin-revision'),
    path('financiera/carteras/<uuid:pk>/ordenes/', orders.preview, name='fin-preview'),
    path('financiera/ordenes/', orders.listado, name='fin-ordenes'),
    path('financiera/ordenes/<uuid:pk>/', orders.detalle, name='fin-orden'),
    path('financiera/ordenes/<uuid:pk>/numero/', orders.numero, name='fin-orden-numero'),
    path('financiera/expedientes/', cases.listado, name='fin-expedientes'),
    path('financiera/expedientes/<uuid:pk>/', cases.detalle, name='fin-expediente'),
    path('financiera/expedientes/<uuid:pk>/transferir/', cases.transferir_view, name='fin-transferir'),
    path('financiera/expedientes/<uuid:pk>/numero/', cases.numero, name='fin-exp-numero'),
    path('financiera/expedientes/<uuid:pk>/cerrar/', cases.cerrar_view, name='fin-cerrar'),
    path('notificaciones/', notif.dashboard, name='notif-dashboard'),
    path('notificaciones/ordenes/', notif.ordenes, name='notif-ordenes'),
    path('notificaciones/ordenes/<uuid:pk>/', notif.orden, name='notif-orden'),
    path('notificaciones/ordenes/<uuid:pk>/lote/', notif.nuevo_lote, name='notif-nuevo-lote'),
    path('notificaciones/lotes/', notif.lotes, name='notif-lotes'),
    path('notificaciones/lotes/<uuid:pk>/', notif.lote, name='notif-lote'),
    path('notificaciones/lotes/<uuid:pk>/imprimir/', maps.listado_imprimible, name='notif-imprimir-lote'),
    path('notificaciones/asignaciones/<uuid:pk>/resultado/', notif.resultado, name='notif-resultado'),
    path('notificaciones/notificadores/', notif.notificadores, name='notif-notificadores'),
    path('notificaciones/notificadores/<int:usuario_id>/casas/', notif.casas_notificadas, name='notif-casas'),
    path('notificaciones/mapa/', maps.mapa, name='notif-mapa'),
    path('auditoria/', audit.historial, name='auditoria'),
    path('documentos/carteras/<uuid:pk>/', docs.cartera_original, name='doc-cartera'),
    path('documentos/preliminares/<uuid:pk>/', docs.preliminar_pdf, name='doc-preliminar'),
    path('api/v1/health/', api.health),
    path('api/v1/ordenes/', api.ordenes),
    path('api/v1/expedientes/', api.expedientes),
    path('api/v1/lotes/', api.lotes),
    path('api/v1/mapa/geojson/', api.mapa_geojson, name='mapa-geojson'),
]
