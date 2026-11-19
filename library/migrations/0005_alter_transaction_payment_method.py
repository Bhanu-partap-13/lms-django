from django.db import migrations, models


def convert_mpesa_to_upi(apps, schema_editor):
    Transaction = apps.get_model("library", "Transaction")
    Transaction.objects.filter(payment_method="mpesa").update(payment_method="upi")


def restore_upi_to_mpesa(apps, schema_editor):
    Transaction = apps.get_model("library", "Transaction")
    Transaction.objects.filter(payment_method="upi").update(payment_method="mpesa")


class Migration(migrations.Migration):

    dependencies = [
        ("library", "0004_alter_member_amount_due"),
    ]

    operations = [
        migrations.AlterField(
            model_name="transaction",
            name="payment_method",
            field=models.CharField(
                choices=[("cash", "Cash"), ("upi", "UPI"), ("card", "Card")],
                max_length=20,
            ),
        ),
        migrations.RunPython(convert_mpesa_to_upi, restore_upi_to_mpesa),
    ]
