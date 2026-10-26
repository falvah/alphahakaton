from html.parser import HTMLParser
from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from credit import services
from credit.demo_scoring import seed_company_source
from credit.models import Application, Company, Document
from credit.pdf import make_pdf


class FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms = []
        self.current = None
        self.nested = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'form':
            self.nested |= self.current is not None
            self.current = {'attrs': attrs, 'controls': []}
            self.forms.append(self.current)
        elif self.current is not None and tag in {'input', 'button', 'select'}:
            self.current['controls'].append(attrs)

    def handle_endtag(self, tag):
        if tag == 'form':
            self.current = None


@override_settings(SANDBOX_MODE=True, STORAGES={
    'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
    'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
}, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'])
class UploadSubmissionTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('upload-owner')
        self.company = Company.objects.create(owner=self.owner, name='Листва',
            representative='Иван Петров', address='Россия, Москва, улица Учебная, 1',
            inn='9900000001', ogrn='1990000000001', business_months=36,
            revenue=2400000, expenses=1600000)
        seed_company_source(self.company)
        self.app = Application.objects.create(company=self.company, product='working',
            amount=1500000, months=24, rate=24, purpose='Пополнение оборотных средств')
        self.url = f'/applications/{self.app.pk}/edit/3/'
        self.client.force_login(self.owner)

    def pdf(self, title='STATEMENT'):
        return SimpleUploadedFile('statement.pdf', make_pdf(title), content_type='application/pdf')

    def send(self, **changes):
        data = {'action': 'submit', 'version': self.app.version,
                'company_version': self.company.version, 'consent': 'on', 'kind': 'income'}
        data.update(changes)
        response = self.client.post(self.url, data)
        self.app.refresh_from_db()
        return response

    def assert_shared_form(self, response):
        parser = FormParser()
        parser.feed(response.content.decode())
        self.assertFalse(parser.nested)
        forms = [f for f in parser.forms if any(c.get('type') == 'file' for c in f['controls'])]
        self.assertEqual(len(forms), 1)
        form = forms[0]
        self.assertEqual(form['attrs'].get('enctype'), 'multipart/form-data')
        controls = form['controls']
        self.assertTrue(any(c.get('name') == 'action' and c.get('value') == 'submit' for c in controls))
        self.assertTrue(any(c.get('name') == 'consent' and 'required' in c for c in controls))
        self.assertTrue(any(c.get('name') == 'action' and c.get('value') == 'upload' and 'formnovalidate' in c for c in controls))
        self.assertFalse(any(c.get('type') == 'file' and 'required' in c for c in controls))

    def test_rendered_form_sends_selected_file_with_submit_button(self):
        self.assert_shared_form(self.client.get(self.url))
        response = self.send(file=self.pdf())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.app.status, 'approved')
        self.assertEqual(self.app.documents.count(), 1)
        self.assertEqual(bytes(self.app.documents.get().content), make_pdf('STATEMENT'))
        self.assertEqual(self.app.events.filter(title='Документ добавлен').count(), 1)

    def test_separate_upload_without_consent_then_submit_without_reupload(self):
        response = self.send(action='upload', consent='', file=self.pdf())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.app.status, 'draft')
        response = self.send()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.app.status, 'approved')
        self.assertEqual(self.app.documents.count(), 1)

    def test_missing_file_explains_error_without_server_failure(self):
        response = self.send()
        self.assertContains(response, 'Добавьте выписку', status_code=400)
        self.assertEqual(self.app.status, 'draft')
        self.assert_shared_form(response)
        self.assertContains(response, 'name="consent" required checked', status_code=400)

    def test_no_consent_retains_uploaded_file_and_allows_retry(self):
        response = self.send(file=self.pdf(), consent='')
        self.assertContains(response, 'Подтвердите согласие', status_code=400)
        self.assertEqual(self.app.documents.count(), 1)
        self.assertEqual(self.app.status, 'draft')
        self.assert_shared_form(response)
        self.assertEqual(self.send().status_code, 302)
        self.assertEqual(self.app.status, 'approved')

    def test_stale_company_version_keeps_document_for_retry(self):
        response = self.send(file=self.pdf(), company_version=999)
        self.assertContains(response, 'Данные компании изменились', status_code=400)
        self.assertEqual(self.app.documents.count(), 1)
        self.assertEqual(self.app.status, 'draft')
        self.assertEqual(self.send().status_code, 302)

    def test_invalid_pdf_and_oversized_pdf_cannot_submit(self):
        for file in [SimpleUploadedFile('bad.pdf', b'<html>bad</html>'),
                     SimpleUploadedFile('large.pdf', b'%PDF-' + b'x' * (5*1024*1024) + b'%%EOF')]:
            with self.subTest(name=file.name):
                response = self.send(file=file)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.app.status, 'draft')
                self.assertFalse(self.app.documents.exists())
                self.assert_shared_form(response)

    def test_duplicate_selected_file_does_not_block_submission_or_duplicate(self):
        self.send(action='upload', file=self.pdf(), consent='')
        response = self.send(file=self.pdf())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.app.status, 'approved')
        self.assertEqual(self.app.documents.count(), 1)
        self.assertEqual(self.app.events.filter(title='Документ добавлен').count(), 1)

    def test_duplicate_selected_file_at_document_limit_can_submit(self):
        for index in range(5):
            app = services.upload(self.app.pk, self.owner, self.app.version, 'income', self.pdf(str(index)))
            self.app = app
        self.assertEqual(self.send(file=self.pdf('0')).status_code, 302)
        self.assertEqual(self.app.documents.count(), 5)
        self.assertEqual(self.app.status, 'approved')

    def test_new_sixth_document_is_rejected_without_losing_existing_files(self):
        for index in range(5):
            self.app = services.upload(self.app.pk, self.owner, self.app.version, 'income', self.pdf(str(index)))
        self.assertEqual(self.send(file=self.pdf('sixth')).status_code, 400)
        self.assertEqual(self.app.documents.count(), 5)
        self.assertEqual(self.app.status, 'draft')
        self.assertEqual(self.send().status_code, 302)

    def test_young_business_has_correct_default_document_and_can_submit(self):
        self.company.business_months = 7
        self.company.save()
        seed_company_source(self.company)
        response = self.client.get(self.url)
        self.assertContains(response, 'value="guarantee" selected')
        self.assertNotContains(response, 'value="income"')
        response = self.send(kind='guarantee', file=self.pdf('GUARANTEE'))
        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(self.app.status, 'draft')
        self.assertEqual(self.app.documents.get().kind, 'guarantee')

    def test_wrong_document_type_explained_before_saving(self):
        response = self.send(kind='guarantee', file=self.pdf())
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.app.documents.exists())
        self.assertEqual(self.app.status, 'draft')

    def test_existing_pdf_with_wrong_type_can_be_corrected_at_submission(self):
        self.company.business_months = 7
        self.company.save()
        seed_company_source(self.company)
        self.app = services.upload(self.app.pk, self.owner, self.app.version, 'income', self.pdf())
        response = self.send(kind='guarantee', file=self.pdf())
        self.assertEqual(response.status_code, 302)
        self.assertNotEqual(self.app.status, 'draft')
        self.assertEqual(self.app.documents.count(), 1)
        self.assertEqual(self.app.documents.get().kind, 'guarantee')
        self.assertTrue(self.app.events.filter(title='Тип документа исправлен').exists())

    def test_stale_application_version_does_not_attach_file(self):
        response = self.send(version=999, file=self.pdf())
        self.assertContains(response, 'Заявка была изменена', status_code=400)
        self.assertFalse(self.app.documents.exists())

    def test_repeated_submission_after_success_does_not_repeat_operations(self):
        self.send(file=self.pdf())
        count = self.app.events.count()
        response = self.send(file=self.pdf())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.app.events.count(), count)
        self.assertEqual(self.app.documents.count(), 1)

    def test_concurrent_change_between_upload_and_submit_is_not_overwritten(self):
        original = services.upload
        def concurrent_upload(*args, **kwargs):
            app = original(*args, **kwargs)
            Application.objects.filter(pk=app.pk).update(version=app.version+1, purpose='Другая цель')
            return app
        with patch('credit.views.services.upload', side_effect=concurrent_upload):
            response = self.send(file=self.pdf())
        self.assertContains(response, 'Заявка была изменена', status_code=400)
        self.assertEqual(self.app.purpose, 'Другая цель')
        self.assertEqual(self.app.status, 'draft')
        self.assertEqual(self.app.documents.count(), 1)
