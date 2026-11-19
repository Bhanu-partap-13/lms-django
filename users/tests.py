from django.test import TestCase
from django.urls import reverse

from .models import Librarian


class ProfileViewTests(TestCase):
    def setUp(self):
        self.user = Librarian.objects.create_user(
            email="profile@example.com",
            password="OldPassword123!",
            first_name="Profile",
            last_name="User",
        )
        self.client.force_login(self.user)

    def test_profile_page_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("profile"))
        self.assertRedirects(response, f"{reverse('login')}?next={reverse('profile')}")

    def test_user_can_update_profile_details(self):
        response = self.client.post(
            reverse("profile"),
            {
                "form_type": "profile",
                "first_name": "Updated",
                "last_name": "Name",
                "email": "updated@example.com",
            },
        )

        self.assertRedirects(response, reverse("profile"))
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Updated")
        self.assertEqual(self.user.last_name, "Name")
        self.assertEqual(self.user.email, "updated@example.com")

    def test_email_must_be_unique(self):
        Librarian.objects.create_user(
            email="used@example.com",
            password="AnotherPassword123!",
            first_name="Other",
            last_name="User",
        )

        response = self.client.post(
            reverse("profile"),
            {
                "form_type": "profile",
                "first_name": "Profile",
                "last_name": "User",
                "email": "used@example.com",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Email already exists")

    def test_user_can_change_password(self):
        response = self.client.post(
            reverse("profile"),
            {
                "form_type": "password",
                "old_password": "OldPassword123!",
                "new_password1": "NewPassword123!",
                "new_password2": "NewPassword123!",
            },
        )

        self.assertRedirects(response, reverse("profile"))
        self.assertTrue(self.client.login(email="profile@example.com", password="NewPassword123!"))
