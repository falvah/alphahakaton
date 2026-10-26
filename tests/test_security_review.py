import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.utils import timezone
from credit import services
from credit.models import Application, Company
from credit.scorecard import score_payload
from credit.scorecard_workflow import evaluate_application, create_seed_application
from .test_scorecard import example


@override_settings(SANDBOX_MODE=True, PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
    STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
              'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}})
class SecurityReviewTests(TestCase):
    def setUp(self):
        self.owner = get_user_model().objects.create_user('security-review-owner')
        self.staff = get_user_model().objects.create_user('security-review-staff', is_staff=True)
        self.company = Company.objects.create(owner=self.owner, name='Компания',
            representative='Иван Петров', address='Россия, Москва, улица Рабочая, 1',
            inn='7701234567', ogrn='1157746123456', business_months=36, revenue=900000)
        self.client.force_login(self.staff)

    def pending(self):
        return Application.objects.create(company=self.company, product='working', amount=1500000,
            months=24, rate=24, purpose='Рефинансирование', status='review',
            assigned_to=self.staff, company_snapshot=services.snapshot(self.company), consent_at=timezone.now())

    def test_applying_stale_or_future_checks_fails_without_partial_changes(self):
        for days in [-31, 1]:
            with self.subTest(days=days):
                app = self.pending()
                payload = example()
                payload['служебное']['дата_оценки'] = (timezone.localdate() + timedelta(days=days)).isoformat()
                with self.assertRaises(ValidationError):
                    evaluate_application(app.pk, self.staff, app.version, payload)
                app.refresh_from_db()
                self.assertEqual(app.status, 'review')
                self.assertEqual(app.scoring, {})
                self.assertFalse(app.events.exists())

    def test_manual_json_cannot_substitute_registration_number(self):
        app = self.pending()
        payload = example()
        payload['заявка']['огрн'] = '1157746123457'
        response = self.client.post(f'/applications/{app.pk}/scorecard/',
            {'version': app.version, 'payload': json.dumps(payload)})
        self.assertContains(response, 'ОГРН JSON должны совпадать', status_code=400)
        app.refresh_from_db()
        self.assertEqual(app.scoring, {})

    def test_imported_applicant_fields_do_not_replace_submitted_identity_or_purpose(self):
        app = self.pending()
        payload = example()
        payload['заявка'].update({'форма': 'АО', 'цель': 'Оборотные средства', 'название': 'Чужая компания'})
        result = evaluate_application(app.pk, self.staff, app.version, payload)
        bound = result.scoring_input['application']
        self.assertEqual(bound['legal_form'], 'ООО')
        self.assertEqual(bound['name'], 'Компания')
        self.assertEqual(bound['purpose'], app.purpose)
        self.assertEqual(result.scoring, score_payload(result.scoring_input))

    def test_manual_approval_requires_scoring_result(self):
        app = self.pending()
        with self.assertRaises(ValidationError):
            services.decide(app.pk, self.staff, app.version,
                {'decision': 'approved', 'note': 'Попытка одобрить заявку без модели.'})
        app.refresh_from_db()
        self.assertEqual(app.status, 'review')
        self.assertFalse(app.events.exists())

    def test_legacy_offer_without_current_card_cannot_be_accepted(self):
        for score in [{}, {'card_version': 'project-1.0.0'}]:
            with self.subTest(score=score):
                app = self.pending()
                app.status = 'approved'
                app.offer_amount, app.offer_months, app.offer_rate = 1500000, 24, 24
                app.scoring = score
                app.save()
                with self.assertRaises(ValidationError):
                    services.accept(app.pk, self.owner, app.version, True)
                app.refresh_from_db()
                self.assertEqual(app.status, 'approved')
                self.assertIsNone(app.accepted_at)

    def test_approved_offer_expired_after_calculation_cannot_be_accepted(self):
        app = create_seed_application(self.owner, example(), True)
        app.checks['valuation_date'] = (timezone.localdate() - timedelta(days=31)).isoformat()
        app.save()
        with self.assertRaises(ValidationError):
            services.accept(app.pk, self.owner, app.version, True)
        app.refresh_from_db()
        self.assertEqual(app.status, 'approved')
        self.assertFalse(app.events.filter(to_status='funded').exists())

    def test_switch_to_production_cannot_accept_sandbox_offer(self):
        app = create_seed_application(self.owner, example(), True)
        with override_settings(SANDBOX_MODE=False), self.assertRaises(ValidationError):
            services.accept(app.pk, self.owner, app.version, True)

    @override_settings(SANDBOX_MODE=False)
    def test_verified_manual_import_can_be_accepted_outside_sandbox(self):
        app = self.pending()
        app = evaluate_application(app.pk, self.staff, app.version, example())
        self.assertFalse(app.checks['simulated'])
        app = services.accept(app.pk, self.owner, app.version, True)
        self.assertEqual(app.status, 'funded')

    def test_conflicting_related_party_counts_cannot_hide_risk_factor(self):
        payload = example()
        payload['обогащение']['related_parties_count'] = 0
        payload['обогащение']['связанные_стороны']['число_связанных'] = 100
        response = self.client.post('/api/v1/scorecard/', payload, content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertIn('дважды', response.json()['error'])

    def test_overlong_password_is_not_silently_truncated_to_valid_prefix(self):
        password = 'a' * 1024
        self.owner.set_password(password)
        self.owner.save()
        self.client.logout()
        response = self.client.post('/login/', {'username': self.owner.username, 'password': password + 'invalid-suffix'})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('_auth_user_id', self.client.session)
        self.assertEqual(self.client.post('/login/', {'username': self.owner.username, 'password': password}).status_code, 302)

    def test_overlong_username_is_not_silently_truncated_to_another_account(self):
        username = 'u' * 150
        get_user_model().objects.create_user(username, password='known-password')
        self.client.logout()
        response = self.client.post('/login/', {'username': username + 'invalid-suffix', 'password': 'known-password'})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn('_auth_user_id', self.client.session)

    def test_production_rejects_debug_tracebacks(self):
        with TemporaryDirectory() as directory:
            env = dict(os.environ, DATA_DIR=directory, SANDBOX_MODE='false', DEBUG='true',
                SECRET_KEY='configuration-test-key-'*4, PYTHONDONTWRITEBYTECODE='1')
            result = subprocess.run([sys.executable, '-c', 'from config import settings'],
                cwd=Path(__file__).resolve().parents[1], env=env, text=True, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('DEBUG must be false', result.stderr)
