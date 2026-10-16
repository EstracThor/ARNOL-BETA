import io
import os
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import openpyxl
from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.gis.geos import MultiPolygon, Polygon
from django.core.exceptions import ValidationError, PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, DatabaseError, transaction
from ardur.apps.catastro.models import CatastroVersion, Predio
from ardur.apps.financiera.models import ImportacionCartera, RegistroCarteraRaw, RegistroCarteraStaging, CarteraDetalle
from ardur.apps.financiera.normalization import HEADERS, normalizar
from ardur.apps.financiera.services import subir, procesar, aprobar, revisar
from ardur.apps.ordenes.services import generar, grupos
from ardur.apps.expedientes.models import Expediente
from ardur.apps.preliminares.services import registrar_preliminar, transferir
from ardur.apps.notificaciones.services import crear_lote, registrar_resultado
from ardur.apps.notificaciones.selectors import expedientes_visibles
from ardur.apps.auditoria.models import EventoAuditoria
from ardur.apps.catastro.services import importar_catastro

class WorkflowTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.fin = cls.user('fin', 'JEFE_FINANCIERA')
        cls.notif = cls.user('notif', 'JEFE_NOTIFICACIONES')
        cls.n1 = cls.user('n1', 'NOTIFICADOR')
        cls.n2 = cls.user('n2', 'NOTIFICADOR')
        cls.consulta = cls.user('consulta', 'CONSULTA')
        cls.version = CatastroVersion.objects.create(nombre='Catastro test', periodo='2026-08', sha256='a' * 64, activo=True)
        cls.p1 = cls.predio('001', '111', '010203', 'PERSONA (+)')
        cls.p2 = cls.predio('002', '222', '010204', 'PERSONA DOS')

    @staticmethod
    def user(name, role):
        user = get_user_model().objects.create_user(username=name, password='Secure-beta-tests-123!')
        group, _ = Group.objects.get_or_create(name=role)
        user.groups.add(group)
        return user

    @classmethod
    def predio(cls, ciu, cedula, clave, nombre):
        poly = Polygon(((660000, 9886000), (660020, 9886000), (660020, 9886020), (660000, 9886020), (660000, 9886000)), srid=32717)
        return Predio.objects.create(version=cls.version, source_fid=ciu, parroquia='01', zona='02', sector='03', clave_ante=clave, cod_client=ciu, ruc_cedula_catastro=cedula, nombres_catastro=nombre, direccion='Dirección test', geom=MultiPolygon(poly, srid=32717))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings_override = override_settings(MEDIA_ROOT=self.tmp.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)

    def excel(self, rows=None, name='cartera.xlsx'):
        book = openpyxl.Workbook()
        book.active.title = 'Sheet 1'
        book.active.append(HEADERS)
        if rows is None:
            rows = [('001', '111', 'PERSONA (+)', '010203', 'DIRECCION', '01', '', '', 50, 2, 0, 0, 3), ('002', '222', 'PERSONA DOS', '010204', 'DIRECCION', '01', '', '', 90, 1, 0, 0, 4)]
        for row in rows:
            book.active.append(row)
        output = io.BytesIO()
        book.save(output)
        return SimpleUploadedFile(name, output.getvalue())

    def importacion(self, rows=None):
        with patch('ardur.apps.financiera.services.enviar_tarea'):
            imp = subir(self.excel(rows), '2026-08', 'Sheet 1', self.fin)
        procesar(imp.pk)
        imp.refresh_from_db()
        return imp

    def casos(self):
        imp = self.importacion()
        aprobar(imp.pk, self.fin)
        order = generar(imp, list(grupos(imp)), self.fin)[0]
        return imp, order, list(order.expedientes.order_by('ciu'))

    def test_same_file_not_duplicated(self):
        upload = self.excel()
        content = upload.read()
        with patch('ardur.apps.financiera.services.enviar_tarea'):
            subir(SimpleUploadedFile('a.xlsx', content), '2026-08', 'Sheet 1', self.fin)
            with self.assertRaises(ValidationError):
                subir(SimpleUploadedFile('b.xlsx', content), '2026-09', 'Sheet 1', self.fin)
        self.assertEqual(ImportacionCartera.objects.count(), 1)

    def test_raw_preserved_and_deceased_not_excluded(self):
        imp, order, cases = self.casos()
        self.assertEqual(order.expedientes.count(), 2)
        self.assertTrue(cases[0].cliente.fallecido)
        raw = RegistroCarteraRaw.objects.get(importacion=imp, numero_fila=2)
        self.assertEqual(raw.datos_originales['CIU'], '001')
        self.assertEqual(raw.datos_originales['NOMBRES'], 'PERSONA (+)')
        self.assertEqual(cases[0].detalle.total_emision, Decimal('50.00'))

    def test_active_process_not_generating_second_case(self):
        _, order, cases = self.casos()
        new_rows = [('001', '111', 'PERSONA (+)', '010203', 'DIRECCION', '01', '', '', 60, 2, 0, 0, 4)]
        imp = self.importacion(new_rows)
        self.assertEqual(imp.raw.get().staging.estado_historico, 'PROCESO_ACTIVO')
        aprobar(imp.pk, self.fin)
        self.assertEqual(dict(grupos(imp)), {})
        with self.assertRaises(ValidationError):
            generar(imp, [('01', '02', '03')], self.fin)
        self.assertEqual(Expediente.objects.count(), 2)

    def test_ambiguous_catastro_requires_review(self):
        self.predio('003', '111', '010203', 'OTRO CANDIDATO')
        imp = self.importacion()
        staging = imp.raw.get(numero_fila=2).staging
        self.assertTrue(staging.requiere_revision)
        self.assertEqual(staging.estado_catastro, 'AMBIGUO')
        self.assertIsNone(staging.predio_id)

    def test_finance_cannot_manage_notifications(self):
        _, order, cases = self.casos()
        self.client.force_login(self.fin)
        self.assertEqual(self.client.get('/notificaciones/').status_code, 403)
        with self.assertRaises(PermissionDenied):
            crear_lote(order, date.today(), self.n1, [cases[0].pk], self.fin)

    def test_notifications_cannot_change_financial_values(self):
        imp = self.importacion()
        self.client.force_login(self.notif)
        self.assertEqual(self.client.post(f'/financiera/carteras/{imp.pk}/aprobar/').status_code, 403)
        with self.assertRaises(PermissionDenied):
            aprobar(imp.pk, self.notif)

    def test_not_visible_until_ready_and_valid_state_transitions(self):
        _, order, cases = self.casos()
        e = cases[0]
        self.assertEqual(expedientes_visibles(self.notif).count(), 0)
        with self.assertRaises(ValidationError):
            transferir(e.pk, self.fin)
        registrar_preliminar(e.pk, self.fin, cantidad=4, recargo=Decimal('1.23'))
        self.assertEqual(expedientes_visibles(self.notif).count(), 0)
        transferir(e.pk, self.fin)
        self.assertEqual(expedientes_visibles(self.notif).count(), 1)
        self.client.force_login(self.notif)
        result = self.client.get('/api/v1/expedientes/').json()['results']
        self.assertEqual([r['id'] for r in result], [str(e.pk)])

    def test_assignment_scope_and_results_audited(self):
        _, order, cases = self.casos()
        for e in cases:
            registrar_preliminar(e.pk, self.fin)
            transferir(e.pk, self.fin)
        lot = crear_lote(order, date.today(), self.n1, [cases[0].pk], self.notif)
        self.assertEqual(expedientes_visibles(self.n1).count(), 1)
        self.assertEqual(expedientes_visibles(self.n2).count(), 0)
        with self.assertRaises(ValidationError):
            crear_lote(order, date.today(), self.n2, [cases[0].pk], self.notif)
        a = lot.asignaciones.get()
        registrar_resultado(a.pk, 'ENTREGADO', 'Recibido', self.notif)
        lot.refresh_from_db()
        cases[0].refresh_from_db()
        self.assertEqual(lot.estado, 'COMPLETADO')
        self.assertEqual(cases[0].estado, 'NOTIFICADO')
        self.assertTrue(EventoAuditoria.objects.filter(accion='RESULTADO_NOTIFICACION').exists())

    def test_geojson_only_selected_order_and_srid(self):
        _, order, cases = self.casos()
        for e in cases:
            registrar_preliminar(e.pk, self.fin)
            transferir(e.pk, self.fin)
        self.assertEqual(self.p1.geom.srid, 32717)
        self.client.force_login(self.notif)
        response = self.client.get('/api/v1/mapa/geojson/', {'orden': order.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()['features']), 2)
        x, y = response.json()['features'][0]['geometry']['coordinates'][0][0][0]
        self.assertTrue(-80 < x < -78 and -2 < y < 0)
        other = type(order).objects.create(parroquia='09', zona='01', sector='03', creado_por=self.fin)
        self.assertEqual(self.client.get('/api/v1/mapa/geojson/', {'orden': other.pk}).status_code, 404)
        self.assertEqual(self.client.get('/api/v1/mapa/geojson/', {'orden': order.pk, 'bbox': '-78,-2,-77,-1'}).json()['features'], [])
        self.assertEqual(self.client.get('/api/v1/mapa/geojson/', {'orden': 'invalido'}).status_code, 404)
        self.assertEqual(self.client.get('/api/v1/mapa/geojson/', {'orden': order.pk, 'bbox': 'NaN,0,1,2'}).status_code, 400)

    def test_duplicate_row_preserved_for_review(self):
        row = ('001', '111', 'PERSONA', '010203', 'D', '01', '', '', 50, 0, 0, 0, 1)
        imp = self.importacion([row, row])
        self.assertEqual(imp.raw.count(), 2)
        self.assertEqual(imp.raw.get(numero_fila=3).staging.estado_validacion, 'REPETIDO')

    def test_database_prevents_duplicate_active_fingerprint(self):
        _, order, cases = self.casos()
        e = cases[0]
        imp = self.importacion([('001', '111', 'PERSONA (+)', '010203', 'D', '01', '', '', 60, 0, 0, 0, 4)])
        aprobar(imp.pk, self.fin)
        unused_detail = CarteraDetalle.objects.get(importacion=imp)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Expediente.objects.create(orden=order, cliente=e.cliente, detalle=unused_detail, predio=e.predio, caso_fingerprint=e.caso_fingerprint)

    def test_order_creation_rolls_back_if_cases_conflict_within_batch(self):
        imp = self.importacion()
        # Simula dos candidatos manualmente revisados que comparten un identificador de caso.
        second = imp.raw.get(numero_fila=3).staging
        RegistroCarteraStaging.objects.filter(pk=second.pk).update(ciu='001')
        aprobar(imp.pk, self.fin)
        with self.assertRaises(ValidationError):
            generar(imp, list(grupos(imp)), self.fin)
        self.assertEqual(Expediente.objects.count(), 0)
        self.assertEqual(CarteraDetalle.objects.filter(importacion=imp).count(), 2)
        from ardur.apps.ordenes.models import Orden
        self.assertEqual(Orden.objects.count(), 0)

    def test_manual_review_keeps_original_and_audits(self):
        self.predio('003', '111', '010203', 'OTRO')
        imp = self.importacion()
        s = imp.raw.get(numero_fila=2).staging
        original = dict(s.raw.datos_originales)
        revisar(s.pk, self.fin, {'total_emision': Decimal('55.00')}, self.p1, 'ACEPTAR', 'Verificado contra catastro')
        s.refresh_from_db()
        self.assertFalse(s.requiere_revision)
        s.raw.refresh_from_db()
        self.assertEqual(s.raw.datos_originales, original)
        self.assertTrue(EventoAuditoria.objects.filter(accion='CORRECCION_MANUAL').exists())

    def test_pages_render_and_private_documents(self):
        imp, order, cases = self.casos()
        self.client.force_login(self.fin)
        urls = ['/financiera/', '/financiera/carteras/', '/financiera/importar/', f'/financiera/carteras/{imp.pk}/', f'/financiera/registros/{imp.raw.first().staging.pk}/', '/financiera/ordenes/', f'/financiera/ordenes/{order.pk}/', '/financiera/expedientes/', f'/financiera/expedientes/{cases[0].pk}/', '/auditoria/']
        for url in urls:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        self.assertRedirects(self.client.get('/financiera/depuracion/'), f'/financiera/carteras/{imp.pk}/')
        self.assertRedirects(self.client.get('/financiera/excepciones/'), f'/financiera/carteras/{imp.pk}/?filtro=revision')
        self.client.logout()
        self.assertEqual(self.client.get(f'/documentos/carteras/{imp.pk}/').status_code, 302)

    def test_csrf_enforced(self):
        from django.test import Client
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.fin)
        imp = self.importacion()
        self.assertEqual(client.post(f'/financiera/carteras/{imp.pk}/aprobar/').status_code, 403)

    def test_raw_and_audit_cannot_be_changed_or_deleted(self):
        imp = self.importacion()
        raw = imp.raw.first()
        with self.assertRaises(DatabaseError), transaction.atomic():
            RegistroCarteraRaw.objects.filter(pk=raw.pk).update(datos_originales={})
        with self.assertRaises(DatabaseError), transaction.atomic():
            RegistroCarteraRaw.objects.filter(pk=raw.pk)._raw_delete('default')
        event = EventoAuditoria.objects.first()
        with self.assertRaises(DatabaseError), transaction.atomic():
            EventoAuditoria.objects.filter(pk=event.pk).update(accion='ALTERADO')
        with self.assertRaises(DatabaseError), transaction.atomic():
            EventoAuditoria.objects.filter(pk=event.pk).delete()

    def test_retry_keeps_raw_and_does_not_duplicate(self):
        imp = self.importacion()
        original_ids = set(imp.raw.values_list('id', flat=True))
        ImportacionCartera.objects.filter(pk=imp.pk).update(estado='ERROR')
        procesar(imp.pk)
        self.assertEqual(set(imp.raw.values_list('id', flat=True)), original_ids)
        self.assertEqual(RegistroCarteraStaging.objects.filter(raw__importacion=imp).count(), 2)

    def test_full_web_flow_after_approval(self):
        imp = self.importacion()
        self.client.force_login(self.fin)
        self.assertEqual(self.client.post(f'/financiera/carteras/{imp.pk}/aprobar/').status_code, 302)
        preview = self.client.get(f'/financiera/carteras/{imp.pk}/ordenes/')
        self.assertEqual(preview.status_code, 200)
        import json
        response = self.client.post(f'/financiera/carteras/{imp.pk}/ordenes/', {'territorios': [json.dumps(['01', '02', '03'])]})
        self.assertEqual(response.status_code, 302)
        cases = list(Expediente.objects.order_by('ciu'))
        self.assertEqual(len(cases), 2)
        order = cases[0].orden
        for e in cases:
            self.assertEqual(self.client.post(f'/financiera/expedientes/{e.pk}/', {'cantidad_titulos': 5, 'recargo_reportado': '0.75'}).status_code, 302)
            self.assertEqual(self.client.post(f'/financiera/expedientes/{e.pk}/transferir/').status_code, 302)
        self.client.force_login(self.notif)
        response = self.client.post(f'/notificaciones/ordenes/{order.pk}/lote/', {'fecha_programada': '2026-10-02', 'notificador': self.n1.pk, 'expedientes': [str(e.pk) for e in cases]})
        self.assertEqual(response.status_code, 302)
        lot = order.lotes.get()
        for url in ['/notificaciones/', '/notificaciones/ordenes/', f'/notificaciones/ordenes/{order.pk}/', '/notificaciones/lotes/', f'/notificaciones/lotes/{lot.pk}/', f'/notificaciones/lotes/{lot.pk}/imprimir/', f'/notificaciones/mapa/?orden={order.pk}&lote={lot.pk}', '/notificaciones/notificadores/', '/auditoria/']:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        a = lot.asignaciones.first()
        self.assertEqual(self.client.post(f'/notificaciones/asignaciones/{a.pk}/resultado/', {'estado': 'ENTREGADO', 'observaciones': 'Recibido'}).status_code, 302)
        a.refresh_from_db()
        self.assertEqual(a.estado, 'ENTREGADO')

    def test_import_catastro_crs_and_duplicate_sha(self):
        # Fixture GeoPackage creado por GDAL/OGR; no usa ni modifica data/input.
        from django.contrib.gis.gdal import GDALException
        from shutil import which
        import subprocess
        ogr = os.environ.get('OGR2OGR_PATH') or which('ogr2ogr') or which('ogr2ogr.exe')
        if not ogr:
            self.fail('ogr2ogr es necesario para validar el importador (incluido en Docker).')
        source = Path(self.tmp.name) / 'source.geojson'
        source.write_text('{"type":"FeatureCollection","features":[{"type":"Feature","properties":{"parroquia":"01","zona":"02","sector":"03","clave_ante":"c1","clave_nuev":"c2"},"geometry":{"type":"Polygon","coordinates":[[[-79.46,-1.03],[-79.459,-1.03],[-79.459,-1.029],[-79.46,-1.029],[-79.46,-1.03]]]}}]}', encoding='utf-8')
        correct = Path(self.tmp.name) / 'correct.gpkg'
        subprocess.run([ogr, '-f', 'GPKG', str(correct), str(source), '-t_srs', 'EPSG:32717', '-nlt', 'MULTIPOLYGON'], check=True, capture_output=True)
        version, count = importar_catastro(correct, '2026-08')
        self.assertEqual(count, 1)
        self.assertEqual(Predio.objects.get(version=version).geom.srid, 32717)
        with self.assertRaises(ValidationError):
            importar_catastro(correct, '2026-09')
        incorrect = Path(self.tmp.name) / 'incorrect.gpkg'
        subprocess.run([ogr, '-f', 'GPKG', str(incorrect), str(source)], check=True, capture_output=True)
        with self.assertRaises(ValidationError):
            importar_catastro(incorrect, '2026-08')

    def test_notified_houses_registry_scope_search_and_corrections(self):
        _, order, cases = self.casos()
        assignments = []
        for case, user in zip(cases, [self.n1, self.n2]):
            registrar_preliminar(case.pk, self.fin)
            transferir(case.pk, self.fin)
            lot = crear_lote(order, date.today(), user, [case.pk], self.notif)
            assignments.append(lot.asignaciones.get())
        url = f'/notificaciones/notificadores/{self.n1.pk}/casas/'
        self.client.force_login(self.n1)
        self.assertContains(self.client.get(url), 'No hay casas notificadas')
        registrar_resultado(assignments[0].pk, 'ENTREGADO', 'Recibido en domicilio', self.notif)
        registrar_resultado(assignments[1].pk, 'ENTREGADO', 'Otro domicilio privado', self.notif)
        response = self.client.get(url)
        self.assertContains(response, 'Recibido en domicilio')
        self.assertNotContains(response, 'Otro domicilio privado')
        self.assertIsNotNone(response.context['page'][0].fecha_registro)
        self.assertEqual(response.context['total_casas'], 1)
        self.assertEqual(self.client.get(f'/notificaciones/notificadores/{self.n2.pk}/casas/').status_code, 404)
        self.assertEqual(self.client.get('/notificaciones/notificadores/').context['notificadores'].count(), 1)
        self.assertEqual(self.client.get(url, {'q': 'inexistente'}).context['total_casas'], 0)
        self.assertEqual(self.client.get(url, {'q': '010203'}).context['total_casas'], 1)
        for user in [self.notif, self.consulta]:
            self.client.force_login(user)
            self.assertEqual(self.client.get(url).status_code, 200)
        self.client.force_login(self.fin)
        self.assertEqual(self.client.get(url).status_code, 403)
        registrar_resultado(assignments[0].pk, 'NO_LOCALIZADO', 'Resultado corregido', self.notif)
        self.client.force_login(self.n1)
        self.assertEqual(self.client.get(url).context['total_casas'], 0)
        self.assertEqual(self.client.get('/notificaciones/notificadores/').context['notificadores'][0].casas_notificadas, 0)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 302)
