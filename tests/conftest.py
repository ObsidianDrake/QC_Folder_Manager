import copy
import time

import pytest
import yaml

from qcmanager.config import load_config
from qcmanager.database import Database
from qcmanager.web import create_app


@pytest.fixture
def configuration(tmp_path):
    root = tmp_path/'runs'
    root.mkdir()
    cfg = {
        'schema_version':1,
        'server':{'public_url':'http://localhost','data_dir':str(tmp_path/'data'),'workers':2},
        'gitea':{'base_url':'http://gitea.test/gitea'},
        'drone':{'base_url':'http://drone.test/drone'},
        'hosts':{'local':{'transport':'local','environment':'zone-a'}},
        'projects':[{'id':'example-project','organization':'ExampleVendor','repository':'Example_QC',
                     'host':'local','run_folder':str(root),'prefix':'qc'}],
    }
    file = tmp_path/'config.yml'
    file.write_text(yaml.safe_dump(cfg))
    return load_config(file)


class FakeGitea:
    def __init__(self):
        self.actor = {'id':1,'login':'vendor-one','is_admin':False,'orgs':['ExampleVendor']}

    def identity(self, token):
        return copy.deepcopy(self.actor)

    def authorize_url(self,state,challenge):
        return 'http://gitea.test/login?state='+state+'&challenge='+challenge

    def exchange(self,code,verifier):
        return 'private-oauth-token'


class FakeDrone:
    def __init__(self):
        self.status='success'
        self.created=100

    def build(self,project,number):
        return {'number':number,'status':self.status,'created':self.created}

    def link(self,project,number):
        return 'http://drone.test/drone/ExampleVendor/Example_QC/'+str(number)


@pytest.fixture
def web(configuration):
    gitea,drone=FakeGitea(),FakeDrone()
    app=create_app(configuration,gitea=gitea,drone=drone)
    app.config['TESTING']=True
    yield app,gitea,drone
    app.extensions['manager'].shutdown()


def login_client(app,gitea):
    client=app.test_client()
    key=app.extensions['sessions'].create({'token':'secret-token','actor':gitea.actor.copy(),'csrf':'test-csrf'},3600)
    client.set_cookie(app.extensions['cookie_name'],key)
    return client,{'X-CSRF-Token':'test-csrf'}


def wait_for(fn, timeout=5):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        result=fn()
        if result:
            return result
        time.sleep(.01)
    raise AssertionError('Background operation did not finish')
