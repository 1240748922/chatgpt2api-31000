"""Test mailbox source selection without creating mailboxes or accounts."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from services import account_replenishment_service as module


def command_for(settings):
    service = module.AccountReplenishmentService(repository=SimpleNamespace(load=lambda: {}))
    return service._build_command(
        python_executable=Path('/fixture/python'), entrypoint=Path('/fixture/register.py'),
        output_dir=Path('/fixture/output'), settings=settings, batch_size=1, batch_id='fixture',
    )


def test_cfworker_selection_does_not_inherit_saved_remail_options():
    command = command_for({'registration_source': 'cfworker', 'remail_service_mode': 'purchase',
                           'cfworker_domain': 'example.invalid'})
    assert '--buy-cfworker-mailbox' in command
    assert '--buy-remail-mailbox' not in command
    assert '--remail-service-mode' not in command


@pytest.mark.parametrize('extra_args', [
    '--buy-remail-mailbox', '--remail-service-mode purchase',
    '--remail-service-mode=code', '--target-at200 2',
])
def test_cfworker_rejects_extra_args_that_silently_select_remail(extra_args):
    with pytest.raises(ValueError, match='extra_args'):
        command_for({'registration_source': 'cfworker', 'extra_args': extra_args})


def test_unrelated_extra_args_are_preserved():
    command = command_for({'registration_source': 'cfworker', 'extra_args': '--proxy-attempts 2'})
    assert command[-2:] == ['--proxy-attempts', '2']


def test_configured_mode_preserves_explicit_cli_source():
    command = command_for({'registration_source': 'configured',
                           'extra_args': '--buy-remail-mailbox --remail-service-mode purchase'})
    assert command[-3:] == ['--buy-remail-mailbox', '--remail-service-mode', 'purchase']
