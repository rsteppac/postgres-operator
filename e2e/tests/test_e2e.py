import json
import unittest
import time
import timeout_decorator
import os
import yaml

from datetime import datetime
from kubernetes import client

from tests.k8s_api import K8s

SPILO_CURRENT = "registry.opensource.zalan.do/acid/spilo-12:1.6-p5"
SPILO_LAZY = "registry.opensource.zalan.do/acid/spilo-cdp-12:1.6-p114"


def to_selector(labels):
    return ",".join(["=".join(lbl) for lbl in labels.items()])


def clean_list(values):
    # value is not stripped bytes, strip and convert to a string
    clean = lambda v: v.strip().decode()
    notNone = lambda v: v

    return list(filter(notNone, map(clean, values)))


class EndToEndTestCase(unittest.TestCase):
    '''
    Test interaction of the operator with multiple K8s components.
    '''

    # `kind` pods may stuck in the `Terminating` phase for a few minutes; hence high test timeout
    TEST_TIMEOUT_SEC = 600

    def eventuallyEqual(self, f, x, m, retries=60, interval=2):
        while True:
            try:
                y = f()
                self.assertEqual(y, x, m.format(y))
                return True
            except AssertionError:
                retries = retries - 1
                if not retries > 0:
                    raise
                time.sleep(interval)

    def eventuallyNotEqual(self, f, x, m, retries=60, interval=2):
        while True:
            try:
                y = f()
                self.assertNotEqual(y, x, m.format(y))
                return True
            except AssertionError:
                retries = retries - 1
                if not retries > 0:
                    raise
                time.sleep(interval)

    def eventuallyTrue(self, f, m, retries=60, interval=2):
        while True:
            try:
                self.assertTrue(f(), m)
                return True
            except AssertionError:
                retries = retries - 1
                if not retries > 0:
                    raise
                time.sleep(interval)

    @classmethod
    @timeout_decorator.timeout(TEST_TIMEOUT_SEC)
    def setUpClass(cls):
        '''
        Deploy operator to a "kind" cluster created by run.sh using examples from /manifests.
        This operator deployment is to be shared among all tests.

        run.sh deletes the 'kind' cluster after successful run along with all operator-related entities.
        In the case of test failure the cluster will stay to enable manual examination;
        next invocation of "make test" will re-create it.
        '''
        print("Test Setup being executed")

        # set a single K8s wrapper for all tests
        k8s = cls.k8s = K8s()

        # remove existing local storage class and create hostpath class
        try:
            k8s.api.storage_v1_api.delete_storage_class("standard")
        except:
            print("Storage class has already been remove")

        # operator deploys pod service account there on start up
        # needed for test_multi_namespace_support()
        cls.namespace = "test"
        try:
            v1_namespace = client.V1Namespace(metadata=client.V1ObjectMeta(name=cls.namespace))
            k8s.api.core_v1.create_namespace(v1_namespace)
        except:
            print("Namespace already present")

        # submit the most recent operator image built on the Docker host
        with open("manifests/postgres-operator.yaml", 'r+') as f:
            operator_deployment = yaml.safe_load(f)
            operator_deployment["spec"]["template"]["spec"]["containers"][0]["image"] = os.environ['OPERATOR_IMAGE']

        with open("manifests/postgres-operator.yaml", 'w') as f:
            yaml.dump(operator_deployment, f, Dumper=yaml.Dumper)

        with open("manifests/configmap.yaml", 'r+') as f:
            configmap = yaml.safe_load(f)
            configmap["data"]["workers"] = "1"

        with open("manifests/configmap.yaml", 'w') as f:
            yaml.dump(configmap, f, Dumper=yaml.Dumper)

        for filename in ["operator-service-account-rbac.yaml",
                         "postgresteam.crd.yaml",
                         "configmap.yaml",
                         "postgres-operator.yaml",
                         "api-service.yaml",
                         "infrastructure-roles.yaml",
                         "infrastructure-roles-new.yaml",
                         "e2e-storage-class.yaml"]:
            result = k8s.create_with_kubectl("manifests/" + filename)
            print("stdout: {}, stderr: {}".format(result.stdout, result.stderr))

        k8s.wait_for_operator_pod_start()

        # reset taints and tolerations
        k8s.api.core_v1.patch_node("postgres-operator-e2e-tests-worker", {"spec": {"taints": []}})
        k8s.api.core_v1.patch_node("postgres-operator-e2e-tests-worker2", {"spec": {"taints": []}})

        # make sure we start a new operator on every new run,
        # this tackles the problem when kind is reused
        # and the Docker image is infact changed (dirty one)

        # patch resync period, this can catch some problems with hanging e2e tests
        # k8s.update_config({"data": {"resync_period":"30s"}},step="TestSuite setup")
        k8s.update_config({}, step="TestSuite Startup")

        actual_operator_image = k8s.api.core_v1.list_namespaced_pod(
            'default', label_selector='name=postgres-operator').items[0].spec.containers[0].image
        print("Tested operator image: {}".format(actual_operator_image))  # shows up after tests finish

        result = k8s.create_with_kubectl("manifests/minimal-postgres-manifest.yaml")
        print('stdout: {}, stderr: {}'.format(result.stdout, result.stderr))
        try:
            k8s.wait_for_pod_start('spilo-role=master')
            k8s.wait_for_pod_start('spilo-role=replica')
        except timeout_decorator.TimeoutError:
            print('Operator log: {}'.format(k8s.get_operator_log()))
            raise

    @timeout_decorator.timeout(TEST_TIMEOUT_SEC)
    def test_enable_disable_connection_pooler(self):
        '''
        For a database without connection pooler, then turns it on, scale up,
        turn off and on again. Test with different ways of doing this (via
        enableConnectionPooler or connectionPooler configuration section). At
        the end turn connection pooler off to not interfere with other tests.
        '''
        k8s = self.k8s
        # service_labels = {
        #     'cluster-name': 'acid-minimal-cluster',
        # }
        # pod_labels = dict({
        #     'connection-pooler': 'acid-minimal-cluster-pooler',
        # })

        # pod_selector = to_selector(pod_labels)
        # service_selector = to_selector(service_labels)

        # enable connection pooler
        k8s.api.custom_objects_api.patch_namespaced_custom_object(
            'acid.zalan.do', 'v1', 'default',
            'postgresqls', 'acid-minimal-cluster',
            {
                'spec': {
                    'enableConnectionPooler': True,
                    'enableReplicaConnectionPooler': True,
                }
            })

        self.eventuallyEqual(lambda: k8s.get_deployment_replica_count(), 2,
                             "Deployment replicas is 2 default")
        self.eventuallyEqual(lambda: k8s.count_running_pods(
                            "connection-pooler=acid-minimal-cluster-pooler"),
                            2, "No pooler pods found")
        self.eventuallyEqual(lambda: k8s.count_running_pods(
                            "connection-pooler=acid-minimal-cluster-pooler-repl"),
                            2, "No pooler replica pods found")
        self.eventuallyEqual(lambda: k8s.count_services_with_label(
                            'application=db-connection-pooler,cluster-name=acid-minimal-cluster'),
                            2, "No pooler service found")

        # Turn off only master connection pooler
        k8s.api.custom_objects_api.patch_namespaced_custom_object(
            'acid.zalan.do', 'v1', 'default',
            'postgresqls', 'acid-minimal-cluster',
            {
                'spec': {
                    'enableConnectionPooler': False,
                    'enableReplicaConnectionPooler': True,
                }
            })

        self.eventuallyEqual(lambda: k8s.get_operator_state(), {"0": "idle"},
                             "Operator does not get in sync")
        self.eventuallyEqual(lambda: k8s.get_deployment_replica_count(), 2,
                             "Deployment replicas is 2 default")
        self.eventuallyEqual(lambda: k8s.count_running_pods(
                             "connection-pooler=acid-minimal-cluster-pooler"),
                             0, "Master pooler pods not deleted")
        self.eventuallyEqual(lambda: k8s.count_running_pods(
                             "connection-pooler=acid-minimal-cluster-pooler-repl"),
                             2, "Pooler replica pods not found")
        self.eventuallyEqual(lambda: k8s.count_services_with_label(
                             'application=db-connection-pooler,cluster-name=acid-minimal-cluster'),
                             1, "No pooler service found")

        # Turn off only replica connection pooler
        k8s.api.custom_objects_api.patch_namespaced_custom_object(
            'acid.zalan.do', 'v1', 'default',
            'postgresqls', 'acid-minimal-cluster',
            {
                'spec': {
                    'enableConnectionPooler': True,
                    'enableReplicaConnectionPooler': False,
                }
            })

        self.eventuallyEqual(lambda: k8s.get_operator_state(), {"0": "idle"},
                             "Operator does not get in sync")
        self.eventuallyEqual(lambda: k8s.get_deployment_replica_count(), 2,
                             "Deployment replicas is 2 default")
        self.eventuallyEqual(lambda: k8s.count_running_pods("connection-pooler=acid-minimal-cluster-pooler"),
                             2, "Master pooler pods not found")
        self.eventuallyEqual(lambda: k8s.count_running_pods("connection-pooler=acid-minimal-cluster-pooler-repl"),
                             0, "Pooler replica pods not deleted")
        self.eventuallyEqual(lambda: k8s.count_services_with_label('application=db-connection-pooler,cluster-name=acid-minimal-cluster'),
                             1, "No pooler service found")

        # scale up connection pooler deployment
        k8s.api.custom_objects_api.patch_namespaced_custom_object(
            'acid.zalan.do', 'v1', 'default',
            'postgresqls', 'acid-minimal-cluster',
            {
                'spec': {
                    'connectionPooler': {
                        'numberOfInstances': 3,
                    },
                }
            })

        self.eventuallyEqual(lambda: k8s.get_deployment_replica_count(), 3,
                             "Deployment replicas is scaled to 3")
        self.eventuallyEqual(lambda: k8s.count_running_pods("connection-pooler=acid-minimal-cluster-pooler"),
                             3, "Scale up of pooler pods does not work")

        # turn it off, keeping config should be overwritten by false
        k8s.api.custom_objects_api.patch_namespaced_custom_object(
            'acid.zalan.do', 'v1', 'default',
            'postgresqls', 'acid-minimal-cluster',
            {
                'spec': {
                    'enableConnectionPooler': False,
                    'enableReplicaConnectionPooler': False,
                }

            })

        self.eventuallyEqual(lambda: k8s.count_running_pods("connection-pooler=acid-minimal-cluster-pooler"),
                             0, "Pooler pods not scaled down")
        self.eventuallyEqual(lambda: k8s.count_services_with_label('application=db-connection-pooler,cluster-name=acid-minimal-cluster'),
                             0, "Pooler service not removed")

        # Verify that all the databases have pooler schema installed.
        # Do this via psql, since otherwise we need to deal with
        # credentials.
        dbList = []

        leader = k8s.get_cluster_leader_pod('acid-minimal-cluster')
        dbListQuery = "select datname from pg_database"
        schemasQuery = """
            select schema_name
            from information_schema.schemata
            where schema_name = 'pooler'
        """
        exec_query = r"psql -tAq -c \"{}\" -d {}"

        if leader:
            try:
                q = exec_query.format(dbListQuery, "postgres")
                q = "su postgres -c \"{}\"".format(q)
                print('Get databases: {}'.format(q))
                result = k8s.exec_with_kubectl(leader.metadata.name, q)
                dbList = clean_list(result.stdout.split(b'\n'))
                print('dbList: {}, stdout: {}, stderr {}'.format(
                    dbList, result.stdout, result.stderr
                ))
            except Exception as ex:
                print('Could not get databases: {}'.format(ex))
                print('Stdout: {}'.format(result.stdout))
                print('Stderr: {}'.format(result.stderr))

            for db in dbList:
                if db in ('template0', 'template1'):
                    continue

                schemas = []
                try:
                    q = exec_query.format(schemasQuery, db)
                    q = "su postgres -c \"{}\"".format(q)
                    print('Get schemas: {}'.format(q))
                    result = k8s.exec_with_kubectl(leader.metadata.name, q)
                    schemas = clean_list(result.stdout.split(b'\n'))
                    print('schemas: {}, stdout: {}, stderr {}'.format(
                        schemas, result.stdout, result.stderr
                    ))
                except Exception as ex:
                    print('Could not get databases: {}'.format(ex))
                    print('Stdout: {}'.format(result.stdout))
                    print('Stderr: {}'.format(result.stderr))

                self.assertNotEqual(len(schemas), 0)
        else:
            print('Could not find leader pod')

        # remove config section to make test work next time
        k8s.api.custom_objects_api.patch_namespaced_custom_object(
            'acid.zalan.do', 'v1', 'default',
            'postgresqls', 'acid-minimal-cluster',
            {
                'spec': {
                    'connectionPooler': None,
                    'EnableReplicaConnectionPooler': False,
                }
            })

    @timeout_decorator.timeout(TEST_TIMEOUT_SEC)
    def test_enable_load_balancer(self):
        '''
        Test if services are updated when enabling/disabling load balancers in Postgres manifest
        '''

        k8s = self.k8s
        cluster_label = 'application=spilo,cluster-name=acid-minimal-cluster,spilo-role={}'

        self.eventuallyEqual(lambda: k8s.get_service_type(cluster_label.format("master")),
                             'ClusterIP',
                             "Expected ClusterIP type initially, found {}")

        try:
            # enable load balancer services
            pg_patch_enable_lbs = {
                "spec": {
                    "enableMasterLoadBalancer": True,
                    "enableReplicaLoadBalancer": True
                }
            }
            k8s.api.custom_objects_api.patch_namespaced_custom_object(
                "acid.zalan.do", "v1", "default", "postgresqls", "acid-minimal-cluster", pg_patch_enable_lbs)

            self.eventuallyEqual(lambda: k8s.get_service_type(cluster_label.format("master")),
                                 'LoadBalancer',
                                 "Expected LoadBalancer service type for master, found {}")

            self.eventuallyEqual(lambda: k8s.get_service_type(cluster_label.format("replica")),
                                 'LoadBalancer',
                                 "Expected LoadBalancer service type for master, found {}")

            # disable load balancer services again
            pg_patch_disable_lbs = {
                "spec": {
                    "enableMasterLoadBalancer": False,
                    "enableReplicaLoadBalancer": False
                }
            }
            k8s.api.custom_objects_api.patch_namespaced_custom_object(
                "acid.zalan.do", "v1", "default", "postgresqls", "acid-minimal-cluster", pg_patch_disable_lbs)

            self.eventuallyEqual(lambda: k8s.get_service_type(cluster_label.format("master")),
                                 'ClusterIP',
                                 "Expected LoadBalancer service type for master, found {}")

            self.eventuallyEqual(lambda: k8s.get_service_type(cluster_label.format("replica")),
                                 'ClusterIP',
                                 "Expected LoadBalancer service type for master, found {}")

        except timeout_decorator.TimeoutError:
            print('Operator log: {}'.format(k8s.get_operator_log()))
            raise

    @timeout_decorator.timeout(TEST_TIMEOUT_SEC)
    def test_infrastructure_roles(self):
        '''
            Test using external secrets for infrastructure roles
        '''
        k8s = self.k8s
        # update infrastructure roles description
        secret_name = "postgresql-infrastructure-roles"
        roles = "secretname: postgresql-infrastructure-roles-new, userkey: user,"\
                "rolekey: memberof, passwordkey: password, defaultrolevalue: robot_zmon"
        patch_infrastructure_roles = {
            "data": {
                "infrastructure_roles_secret_name": secret_name,
                "infrastructure_roles_secrets": roles,
            },
        }
        k8s.update_config(patch_infrastructure_roles)
        self.eventuallyEqual(lambda: k8s.get_operator_state(), {"0": "idle"},
                             "Operator does not get in sync")

        try:
            # check that new roles are represented in the config by requesting the
            # operator configuration via API

            def verify_role():
                try:
                    operator_pod = k8s.get_operator_pod()
                    get_config_cmd = "wget --quiet -O - localhost:8080/config"
                    result = k8s.exec_with_kubectl(operator_pod.metadata.name,
                                                   get_config_cmd)
                    try:
                        roles_dict = (json.loads(result.stdout)
                                          .get("controller", {})
                                          .get("InfrastructureRoles"))
                    except:
                        return False

                    if "robot_zmon_acid_monitoring_new" in roles_dict:
                        role = roles_dict["robot_zmon_acid_monitoring_new"]
                        role.pop("Password", None)
                        self.assertDictEqual(role, {
                            "Name": "robot_zmon_acid_monitoring_new",
                            "Flags": None,
                            "MemberOf": ["robot_zmon"],
                            "Parameters": None,
                            "AdminRole": "",
                            "Origin": 2,
                        })
                        return True
                except:
                    pass

                return False

            self.eventuallyTrue(verify_role, "infrastructure role setup is not loaded")

        except timeout_decorator.TimeoutError:
            print('Operator log: {}'.format(k8s.get_operator_log()))
            raise

    @timeout_decorator.timeout(TEST_TIMEOUT_SEC)
    def test_lazy_spilo_upgrade(self):
        '''
        Test lazy upgrade for the Spilo image: operator changes a stateful set
        but lets pods run with the old image until they are recreated for
        reasons other than operator's activity. That works because the operator
        configures stateful sets to use "onDelete" pod update policy.

        The test covers:
        1) enabling lazy upgrade in existing operator deployment
        2) forcing the normal rolling upgrade by changing the operator
        configmap and restarting its pod
        '''

        k8s = self.k8s

        pod0 = 'acid-minimal-cluster-0'
        pod1 = 'acid-minimal-cluster-1'

        self.eventuallyEqual(lambda: k8s.count_running_pods(), 2,
                             "No 2 pods running")
        self.eventuallyEqual(lambda: len(k8s.get_patroni_running_members(pod0)),
                             2, "Postgres status did not enter running")

        patch_lazy_spilo_upgrade = {
            "data": {
                "docker_image": SPILO_CURRENT,
                "enable_lazy_spilo_upgrade": "false"
            }
        }
        k8s.update_config(patch_lazy_spilo_upgrade,
                          step="Init baseline image version")

        self.eventuallyEqual(lambda: k8s.get_statefulset_image(), SPILO_CURRENT,
                             "Statefulset not updated initially")
        self.eventuallyEqual(lambda: k8s.count_running_pods(), 2,
                             "No 2 pods running")
        self.eventuallyEqual(lambda: len(k8s.get_patroni_running_members(pod0)),
                             2, "Postgres status did not enter running")

        self.eventuallyEqual(lambda: k8s.get_effective_pod_image(pod0),
                             SPILO_CURRENT, "Rolling upgrade was not executed")
        self.eventuallyEqual(lambda: k8s.get_effective_pod_image(pod1),
                             SPILO_CURRENT, "Rolling upgrade was not executed")

        # update docker image in config and enable the lazy upgrade
        conf_image = SPILO_LAZY
        patch_lazy_spilo_upgrade = {
            "data": {
                "docker_image": conf_image,
                "enable_lazy_spilo_upgrade": "true"
            }
        }
        k8s.update_config(patch_lazy_spilo_upgrade,
                          step="patch image and lazy upgrade")
        self.eventuallyEqual(lambda: k8s.get_statefulset_image(), conf_image,
                             "Statefulset not updated to next Docker image")

        try:
            # restart the pod to get a container with the new image
            k8s.api.core_v1.delete_namespaced_pod(pod0, 'default')

            # verify only pod-0 which was deleted got new image from statefulset
            self.eventuallyEqual(lambda: k8s.get_effective_pod_image(pod0),
                                 conf_image, "Delete pod-0 did not get new spilo image")
            self.eventuallyEqual(lambda: k8s.count_running_pods(), 2,
                                 "No two pods running after lazy rolling upgrade")
            self.eventuallyEqual(lambda: len(k8s.get_patroni_running_members(pod0)),
                                 2, "Postgres status did not enter running")
            self.assertNotEqual(lambda: k8s.get_effective_pod_image(pod1),
                                SPILO_CURRENT,
                                "pod-1 should not have change Docker image to {}".format(SPILO_CURRENT))

            # clean up
            unpatch_lazy_spilo_upgrade = {
                "data": {
                    "enable_lazy_spilo_upgrade": "false",
                }
            }
            k8s.update_config(unpatch_lazy_spilo_upgrade, step="patch lazy upgrade")

            # at this point operator will complete the normal rolling upgrade
            # so we additonally test if disabling the lazy upgrade - forcing the normal rolling upgrade - works
            self.eventuallyEqual(lambda: k8s.get_effective_pod_image(pod0),
                                 conf_image, "Rolling upgrade was not executed",
                                 50, 3)
            self.eventuallyEqual(lambda: k8s.get_effective_pod_image(pod1),
                                 conf_image, "Rolling upgrade was not executed",
                                 50, 3)
            self.eventuallyEqual(lambda: len(k8s.get_patroni_running_members(pod0)),
                                 2, "Postgres status did not enter running")

        except timeout_decorator.TimeoutError:
            print('Operator log: {}'.format(k8s.get_operator_log()))
            raise

    @timeout_decorator.timeout(TEST_TIMEOUT_SEC)
    def test_logical_backup_cron_job(self):
        '''
        Ensure we can (a) create the cron job at user request for a specific PG cluster
                      (b) update the cluster-wide image for the logical backup pod
                      (c) delete the job at user request

        Limitations:
        (a) Does not run the actual batch job because there is no S3 mock to upload backups to
        (b) Assumes 'acid-minimal-cluster' exists as defined in setUp
        '''

        k8s = self.k8s

        # create the cron job
        schedule = "7 7 7 7 *"
        pg_patch_enable_backup = {
            "spec": {
                "enableLogicalBackup": True,
                "logicalBackupSchedule": schedule
            }
        }
        k8s.api.custom_objects_api.patch_namespaced_custom_object(
            "acid.zalan.do", "v1", "default", "postgresqls", "acid-minimal-cluster", pg_patch_enable_backup)

        try:
            self.eventuallyEqual(lambda: len(k8s.get_logical_backup_job().items), 1, "failed to create logical backup job")

            job = k8s.get_logical_backup_job().items[0]
            self.assertEqual(job.metadata.name, "logical-backup-acid-minimal-cluster",
                             "Expected job name {}, found {}"
                             .format("logical-backup-acid-minimal-cluster", job.metadata.name))
            self.assertEqual(job.spec.schedule, schedule,
                             "Expected {} schedule, found {}"
                             .format(schedule, job.spec.schedule))

            # update the cluster-wide image of the logical backup pod
            image = "test-image-name"
            patch_logical_backup_image = {
                "data": {
                    "logical_backup_docker_image": image,
                }
            }
            k8s.update_config(patch_logical_backup_image, step="patch logical backup image")

            def get_docker_image():
                jobs = k8s.get_logical_backup_job().items
                return jobs[0].spec.job_template.spec.template.spec.containers[0].image

            self.eventuallyEqual(get_docker_image, image,
                                 "Expected job image {}, found {}".format(image, "{}"))

            # delete the logical backup cron job
            pg_patch_disable_backup = {
                "spec": {
                    "enableLogicalBackup": False,
                }
            }
            k8s.api.custom_objects_api.patch_namespaced_custom_object(
                "acid.zalan.do", "v1", "default", "postgresqls", "acid-minimal-cluster", pg_patch_disable_backup)

            self.eventuallyEqual(lambda: len(k8s.get_logical_backup_job().items), 0, "failed to create logical backup job")

        except timeout_decorator.TimeoutError:
            print('Operator log: {}'.format(k8s.get_operator_log()))
            raise

        # ensure cluster is healthy after tests
        self.eventuallyEqual(lambda: len(k8s.get_patroni_running_members("acid-minimal-cluster-0")), 2, "Postgres status did not enter running")

    @timeout_decorator.timeout(TEST_TIMEOUT_SEC)
    def test_min_resource_limits(self):
        '''
        Lower resource limits below configured minimum and let operator fix it
        '''
        k8s = self.k8s
        # self.eventuallyEqual(lambda: k8s.pg_get_status(), "Running", "Cluster not healthy at start")

        # configure minimum boundaries for CPU and memory limits
        minCPULimit = '503m'
        minMemoryLimit = '502Mi'

        patch_min_resource_limits = {
            "data": {
                "min_cpu_limit": minCPULimit,
                "min_memory_limit": minMemoryLimit
            }
        }

        # lower resource limits below minimum
        pg_patch_resources = {
            "spec": {
                "resources": {
                    "requests": {
                        "cpu": "10m",
                        "memory": "50Mi"
                    },
                    "limits": {
                        "cpu": "200m",
                        "memory": "200Mi"
                    }
                }
            }
        }
        k8s.api.custom_objects_api.patch_namespaced_custom_object(
            "acid.zalan.do", "v1", "default", "postgresqls", "acid-minimal-cluster", pg_patch_resources)

        k8s.patch_statefulset({"metadata": {"annotations": {"zalando-postgres-operator-rolling-update-required": "False"}}})
        k8s.update_config(patch_min_resource_limits, "Minimum resource test")

        self.eventuallyEqual(lambda: k8s.count_running_pods(), 2, "No two pods running after lazy rolling upgrade")
        self.eventuallyEqual(lambda: len(k8s.get_patroni_running_members()), 2, "Postgres status did not enter running")

        def verify_pod_limits():
            pods = k8s.api.core_v1.list_namespaced_pod('default', label_selector="cluster-name=acid-minimal-cluster,application=spilo").items
            if len(pods) < 2:
                return False

            r = pods[0].spec.containers[0].resources.limits['memory'] == minMemoryLimit
            r = r and pods[0].spec.containers[0].resources.limits['cpu'] == minCPULimit
            r = r and pods[1].spec.containers[0].resources.limits['memory'] == minMemoryLimit
            r = r and pods[1].spec.containers[0].resources.limits['cpu'] == minCPULimit
            return r

        self.eventuallyTrue(verify_pod_limits, "Pod limits where not adjusted")

    @classmethod
    def setUp(cls):
        # cls.k8s.update_config({}, step="Setup")
        cls.k8s.patch_statefulset({"meta": {"annotations": {"zalando-postgres-operator-rolling-update-required": False}}})
        pass

    @timeout_decorator.timeout(TEST_TIMEOUT_SEC)
    def test_multi_namespace_support(self):
        '''
        Create a customized Postgres cluster in a non-default namespace.
        '''
        k8s = self.k8s

        with open("manifests/complete-postgres-manifest.yaml", 'r+') as f:
            pg_manifest = yaml.safe_load(f)
            pg_manifest["metadata"]["namespace"] = self.namespace
            yaml.dump(pg_manifest, f, Dumper=yaml.Dumper)

        try:
            k8s.create_with_kubectl("manifests/complete-postgres-manifest.yaml")
            k8s.wait_for_pod_start("spilo-role=master", self.namespace)
            self.assert_master_is_unique(self.namespace, "acid-test-cluster")

        except timeout_decorator.TimeoutError:
            print('Operator log: {}'.format(k8s.get_operator_log()))
            raise

    @timeout_decorator.timeout(TEST_TIMEOUT_SEC)
    def test_zz_node_readiness_label(self):
        '''
           Remove node readiness label from master node. This must cause a failover.
        '''
        k8s = self.k8s
        cluster_label = 'application=spilo,cluster-name=acid-minimal-cluster'
        readiness_label = 'lifecycle-status'
        readiness_value = 'ready'

        try:
            # get nodes of master and replica(s) (expected target of new master)
            current_master_node, current_replica_nodes = k8s.get_pg_nodes(cluster_label)
            num_replicas = len(current_replica_nodes)
            failover_targets = self.get_failover_targets(current_master_node, current_replica_nodes)

            # add node_readiness_label to potential failover nodes
            patch_readiness_label = {
                "metadata": {
                    "labels": {
                        readiness_label: readiness_value
                    }
                }
            }
            self.assertTrue(len(failover_targets) > 0, "No failover targets available")
            for failover_target in failover_targets:
                k8s.api.core_v1.patch_node(failover_target, patch_readiness_label)

            # define node_readiness_label in config map which should trigger a failover of the master
            patch_readiness_label_config = {
                "data": {
                    "node_readiness_label": readiness_label + ':' + readiness_value,
                }
            }
            k8s.update_config(patch_readiness_label_config, "setting readiness label")
            new_master_node, new_replica_nodes = self.assert_failover(
                current_master_node, num_replicas, failover_targets, cluster_label)

            # patch also node where master ran before
            k8s.api.core_v1.patch_node(current_master_node, patch_readiness_label)

            # toggle pod anti affinity to move replica away from master node
            self.eventuallyTrue(lambda: self.assert_distributed_pods(new_master_node, new_replica_nodes, cluster_label), "Pods are redistributed")

        except timeout_decorator.TimeoutError:
            print('Operator log: {}'.format(k8s.get_operator_log()))
            raise

    @timeout_decorator.timeout(TEST_TIMEOUT_SEC)
    def test_scaling(self):
        '''
           Scale up from 2 to 3 and back to 2 pods by updating the Postgres manifest at runtime.
        '''
        k8s = self.k8s
        pod = "acid-minimal-cluster-0"

        k8s.scale_cluster(3)
        self.eventuallyEqual(lambda: k8s.count_running_pods(), 3, "Scale up to 3 failed")
        self.eventuallyEqual(lambda: len(k8s.get_patroni_running_members(pod)), 3, "Not all 3 nodes healthy")

        k8s.scale_cluster(2)
        self.eventuallyEqual(lambda: k8s.count_running_pods(), 2, "Scale down to 2 failed")
        self.eventuallyEqual(lambda: len(k8s.get_patroni_running_members(pod)), 2, "Not all members 2 healthy")

    @timeout_decorator.timeout(TEST_TIMEOUT_SEC)
    def test_service_annotations(self):
        '''
            Create a Postgres cluster with service annotations and check them.
        '''
        k8s = self.k8s
        patch_custom_service_annotations = {
            "data": {
                "custom_service_annotations": "foo:bar",
            }
        }
        k8s.update_config(patch_custom_service_annotations)

        pg_patch_custom_annotations = {
            "spec": {
                "serviceAnnotations": {
                    "annotation.key": "value",
                    "alice": "bob",
                }
            }
        }
        k8s.api.custom_objects_api.patch_namespaced_custom_object(
            "acid.zalan.do", "v1", "default", "postgresqls", "acid-minimal-cluster", pg_patch_custom_annotations)

        annotations = {
            "annotation.key": "value",
            "foo": "bar",
            "alice": "bob"
        }

        self.eventuallyTrue(lambda: k8s.check_service_annotations("cluster-name=acid-minimal-cluster,spilo-role=master", annotations), "Wrong annotations")
        self.eventuallyTrue(lambda: k8s.check_service_annotations("cluster-name=acid-minimal-cluster,spilo-role=replica", annotations), "Wrong annotations")

        # clean up
        unpatch_custom_service_annotations = {
            "data": {
                "custom_service_annotations": "",
            }
        }
        k8s.update_config(unpatch_custom_service_annotations)

    @timeout_decorator.timeout(TEST_TIMEOUT_SEC)
    def test_statefulset_annotation_propagation(self):
        '''
           Inject annotation to Postgresql CRD and check it's propagation to stateful set
        '''
        k8s = self.k8s
        cluster_label = 'application=spilo,cluster-name=acid-minimal-cluster'

        patch_sset_propagate_annotations = {
            "data": {
                "downscaler_annotations": "deployment-time,downscaler/*",
            }
        }
        k8s.update_config(patch_sset_propagate_annotations)

        pg_crd_annotations = {
            "metadata": {
                "annotations": {
                    "deployment-time": "2020-04-30 12:00:00",
                    "downscaler/downtime_replicas": "0",
                },
            }
        }
        k8s.api.custom_objects_api.patch_namespaced_custom_object(
            "acid.zalan.do", "v1", "default", "postgresqls", "acid-minimal-cluster", pg_crd_annotations)

        annotations = {
            "deployment-time": "2020-04-30 12:00:00",
            "downscaler/downtime_replicas": "0",
        }

        self.eventuallyTrue(lambda: k8s.check_statefulset_annotations(cluster_label, annotations), "Annotations missing")

    @timeout_decorator.timeout(TEST_TIMEOUT_SEC)
    @unittest.skip("Skipping this test until fixed")
    def test_zzz_taint_based_eviction(self):
        '''
           Add taint "postgres=:NoExecute" to node with master. This must cause a failover.
        '''
        k8s = self.k8s
        cluster_label = 'application=spilo,cluster-name=acid-minimal-cluster'

        # verify we are in good state from potential previous tests
        self.eventuallyEqual(lambda: k8s.count_running_pods(), 2, "No 2 pods running")
        self.eventuallyEqual(lambda: len(k8s.get_patroni_running_members("acid-minimal-cluster-0")), 2, "Postgres status did not enter running")

        # get nodes of master and replica(s) (expected target of new master)
        master_nodes, replica_nodes = k8s.get_cluster_nodes()

        self.assertNotEqual(master_nodes, [])
        self.assertNotEqual(replica_nodes, [])

        # taint node with postgres=:NoExecute to force failover
        body = {
            "spec": {
                "taints": [
                    {
                        "effect": "NoExecute",
                        "key": "postgres"
                    }
                ]
            }
        }

        k8s.api.core_v1.patch_node(master_nodes[0], body)
        self.eventuallyTrue(lambda: k8s.get_cluster_nodes()[0], replica_nodes)
        self.assertNotEqual(lambda: k8s.get_cluster_nodes()[0], master_nodes)

        # add toleration to pods
        patch_toleration_config = {
            "data": {
                "toleration": "key:postgres,operator:Exists,effect:NoExecute"
            }
        }

        k8s.update_config(patch_toleration_config, step="allow tainted nodes")

        self.eventuallyEqual(lambda: k8s.count_running_pods(), 2, "No 2 pods running")
        self.eventuallyEqual(lambda: len(k8s.get_patroni_running_members("acid-minimal-cluster-0")), 2, "Postgres status did not enter running")

        # toggle pod anti affinity to move replica away from master node
        nm, new_replica_nodes = k8s.get_cluster_nodes()
        new_master_node = nm[0]
        self.assert_distributed_pods(new_master_node, new_replica_nodes, cluster_label)

    @timeout_decorator.timeout(TEST_TIMEOUT_SEC)
    def test_zzzz_cluster_deletion(self):
        '''
           Test deletion with configured protection
        '''
        k8s = self.k8s
        cluster_label = 'application=spilo,cluster-name=acid-minimal-cluster'

        # configure delete protection
        patch_delete_annotations = {
            "data": {
                "delete_annotation_date_key": "delete-date",
                "delete_annotation_name_key": "delete-clustername"
            }
        }
        k8s.update_config(patch_delete_annotations)
        time.sleep(15)
        self.eventuallyEqual(lambda: k8s.get_operator_state(), {"0": "idle"}, "Operator does not get in sync")

        try:
            # this delete attempt should be omitted because of missing annotations
            k8s.api.custom_objects_api.delete_namespaced_custom_object(
                "acid.zalan.do", "v1", "default", "postgresqls", "acid-minimal-cluster")
            time.sleep(5)
            self.eventuallyEqual(lambda: k8s.get_operator_state(), {"0": "idle"}, "Operator does not get in sync")

            # check that pods and services are still there
            k8s.wait_for_running_pods(cluster_label, 2)
            k8s.wait_for_service(cluster_label)

            # recreate Postgres cluster resource
            k8s.create_with_kubectl("manifests/minimal-postgres-manifest.yaml")

            # wait a little before proceeding
            time.sleep(10)
            self.eventuallyEqual(lambda: k8s.get_operator_state(), {"0": "idle"}, "Operator does not get in sync")

            # add annotations to manifest
            delete_date = datetime.today().strftime('%Y-%m-%d')
            pg_patch_delete_annotations = {
                "metadata": {
                    "annotations": {
                        "delete-date": delete_date,
                        "delete-clustername": "acid-minimal-cluster",
                    }
                }
            }
            k8s.api.custom_objects_api.patch_namespaced_custom_object(
                "acid.zalan.do", "v1", "default", "postgresqls", "acid-minimal-cluster", pg_patch_delete_annotations)
            self.eventuallyEqual(lambda: k8s.get_operator_state(), {"0": "idle"}, "Operator does not get in sync")

            # wait a little before proceeding
            time.sleep(20)
            k8s.wait_for_running_pods(cluster_label, 2)
            k8s.wait_for_service(cluster_label)

            # now delete process should be triggered
            k8s.api.custom_objects_api.delete_namespaced_custom_object(
                "acid.zalan.do", "v1", "default", "postgresqls", "acid-minimal-cluster")

            self.eventuallyEqual(lambda: len(k8s.api.custom_objects_api.list_namespaced_custom_object(
                "acid.zalan.do", "v1", "default", "postgresqls", label_selector="cluster-name=acid-minimal-cluster")["items"]), 0, "Manifest not deleted")

            # check if everything has been deleted
            self.eventuallyEqual(lambda: k8s.count_pods_with_label(cluster_label), 0, "Pods not deleted")
            self.eventuallyEqual(lambda: k8s.count_services_with_label(cluster_label), 0, "Service not deleted")
            self.eventuallyEqual(lambda: k8s.count_endpoints_with_label(cluster_label), 0, "Endpoints not deleted")
            self.eventuallyEqual(lambda: k8s.count_statefulsets_with_label(cluster_label), 0, "Statefulset not deleted")
            self.eventuallyEqual(lambda: k8s.count_deployments_with_label(cluster_label), 0, "Deployments not deleted")
            self.eventuallyEqual(lambda: k8s.count_pdbs_with_label(cluster_label), 0, "Pod disruption budget not deleted")
            self.eventuallyEqual(lambda: k8s.count_secrets_with_label(cluster_label), 0, "Secrets not deleted")

        except timeout_decorator.TimeoutError:
            print('Operator log: {}'.format(k8s.get_operator_log()))
            raise

        # reset configmap
        patch_delete_annotations = {
            "data": {
                "delete_annotation_date_key": "",
                "delete_annotation_name_key": ""
            }
        }
        k8s.update_config(patch_delete_annotations)

    def get_failover_targets(self, master_node, replica_nodes):
        '''
           If all pods live on the same node, failover will happen to other worker(s)
        '''
        k8s = self.k8s
        k8s_master_exclusion = 'kubernetes.io/hostname!=postgres-operator-e2e-tests-control-plane'

        failover_targets = [x for x in replica_nodes if x != master_node]
        if len(failover_targets) == 0:
            nodes = k8s.api.core_v1.list_node(label_selector=k8s_master_exclusion)
            for n in nodes.items:
                if n.metadata.name != master_node:
                    failover_targets.append(n.metadata.name)

        return failover_targets

    def assert_failover(self, current_master_node, num_replicas, failover_targets, cluster_label):
        '''
           Check if master is failing over. The replica should move first to be the switchover target
        '''
        k8s = self.k8s
        k8s.wait_for_pod_failover(failover_targets, 'spilo-role=master,' + cluster_label)
        k8s.wait_for_pod_start('spilo-role=replica,' + cluster_label)

        new_master_node, new_replica_nodes = k8s.get_pg_nodes(cluster_label)
        self.assertNotEqual(current_master_node, new_master_node,
                            "Master on {} did not fail over to one of {}".format(current_master_node, failover_targets))
        self.assertEqual(num_replicas, len(new_replica_nodes),
                         "Expected {} replicas, found {}".format(num_replicas, len(new_replica_nodes)))
        self.assert_master_is_unique()

        return new_master_node, new_replica_nodes

    def assert_master_is_unique(self, namespace='default', clusterName="acid-minimal-cluster"):
        '''
           Check that there is a single pod in the k8s cluster with the label "spilo-role=master"
           To be called manually after operations that affect pods
        '''
        k8s = self.k8s
        labels = 'spilo-role=master,cluster-name=' + clusterName

        num_of_master_pods = k8s.count_pods_with_label(labels, namespace)
        self.assertEqual(num_of_master_pods, 1, "Expected 1 master pod, found {}".format(num_of_master_pods))

    def assert_distributed_pods(self, master_node, replica_nodes, cluster_label):
        '''
           Other tests can lead to the situation that master and replica are on the same node.
           Toggle pod anti affinty to distribute pods accross nodes (replica in particular).
        '''
        k8s = self.k8s
        failover_targets = self.get_failover_targets(master_node, replica_nodes)

        # enable pod anti affintiy in config map which should trigger movement of replica
        patch_enable_antiaffinity = {
            "data": {
                "enable_pod_antiaffinity": "true"
            }
        }
        k8s.update_config(patch_enable_antiaffinity, "enable antiaffinity")
        self.assert_failover(master_node, len(replica_nodes), failover_targets, cluster_label)

        # now disable pod anti affintiy again which will cause yet another failover
        patch_disable_antiaffinity = {
            "data": {
                "enable_pod_antiaffinity": "false"
            }
        }
        k8s.update_config(patch_disable_antiaffinity, "disalbe antiaffinity")
        k8s.wait_for_pod_start('spilo-role=master')
        k8s.wait_for_pod_start('spilo-role=replica')
        return True


if __name__ == '__main__':
    unittest.main()
