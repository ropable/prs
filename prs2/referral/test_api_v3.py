from django.urls import reverse
from referral.test_models import PrsTestCase

API_MODELS_LOOKUP = [
    "referraltype",
    "region",
    "organisation",
    "taskstate",
    "tasktype",
    "user",
    "tag",
]
API_MODELS = [
    "referral",
    "task",
    "clearance",
]


class PrsAPITest(PrsTestCase):
    def test_permission_resource_list(self):
        """Test auth and anon access permission to resource lists"""
        for model in API_MODELS_LOOKUP + API_MODELS:
            url = reverse(f"api:{model}_api_resource")
            self.client.logout()
            response = self.client.get(url)  # Anonymous user
            self.assertEqual(response.status_code, 200)
            self.client.login(username="normaluser", password="pass")
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.client.login(username="readonlyuser", password="pass")
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)

    def test_permission_resource_detail(self):
        """Test auth and anon access permission to resource details"""
        for model in API_MODELS_LOOKUP:
            url = reverse(f"api:{model}_api_resource")
            self.client.login(username="normaluser", password="pass")
            response = self.client.get(url)
            res_list = response.json()
            obj_id = res_list[0]["id"]
            url = reverse(f"api:{model}_api_resource", kwargs={"pk": obj_id})
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.client.logout()
            self.client.login(username="readonlyuser", password="pass")
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.client.logout()
            response = self.client.get(url)  # Anonymous user
            self.assertEqual(response.status_code, 200)
        # The API response is a bit different for these models.
        # TODO: test filtering and pagination.
        for model in API_MODELS:
            url = reverse(f"api:{model}_api_resource")
            self.client.login(username="normaluser", password="pass")
            response = self.client.get(url)
            res_list = response.json()
            obj_id = res_list["objects"][0]["id"]
            url = reverse(f"api:{model}_api_resource", kwargs={"pk": obj_id})
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.client.logout()
            self.client.login(username="readonlyuser", password="pass")
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            self.client.logout()
            response = self.client.get(url)  # Anonymous user
            self.assertEqual(response.status_code, 200)
