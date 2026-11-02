from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from qcmanager import clients
from qcmanager.config import ConfigError, load_config


@pytest.mark.parametrize('section,key', [('gitea','client_id'), ('gitea','client_secret'), ('drone','token')])
@pytest.mark.parametrize('value', [None, '', '   ', 123, 'private-secret\ninvalid'])
def test_invalid_credential_names_field_without_value(configuration, tmp_path, section, key, value):
    configuration[section][key] = value
    path = tmp_path/'invalid.yml'
    path.write_text(yaml.safe_dump(configuration))
    with pytest.raises(ConfigError) as error:
        load_config(path)
    assert section+'.'+key in str(error.value)
    assert 'private-secret' not in str(error.value)


def test_missing_credential_names_field(configuration, tmp_path):
    del configuration['drone']['token']
    path = tmp_path/'missing.yml'
    path.write_text(yaml.safe_dump(configuration))
    with pytest.raises(ConfigError, match=r'drone\.token'):
        load_config(path)


def test_clients_use_direct_values(configuration, monkeypatch):
    for name in ('QC_FOLDER_GITEA_CLIENT_ID', 'QC_FOLDER_GITEA_CLIENT_SECRET', 'QC_FOLDER_DRONE_TOKEN'):
        monkeypatch.setenv(name, 'must-not-be-used')
    calls = []
    def fetch(url, token=None, form=None):
        calls.append((url, token, form))
        if form is not None:
            return {'access_token':'user-token'}
        return {'number':1234, 'status':'success'}
    monkeypatch.setattr(clients, 'http_json', fetch)
    gitea = clients.Gitea(configuration)
    query = parse_qs(urlsplit(gitea.authorize_url('state', 'challenge')).query)
    assert query['client_id'] == ['test-client']
    assert gitea.exchange('code', 'verifier') == 'user-token'
    assert calls[0][2]['client_id'] == 'test-client'
    assert calls[0][2]['client_secret'] == 'test-secret'
    clients.Drone(configuration).build(configuration['projects'][0], 1234)
    assert calls[1][1] == 'test-drone-token'
