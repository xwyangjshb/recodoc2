from __future__ import unicode_literals
import os
import time
import shutil
import unittest
from django.test import TestCase, TransactionTestCase
from django.conf import settings
from django.db import transaction
from py4j.java_gateway import JavaGateway
from docutil.test_util import clean_test_dir
from codebase.models import CodeBase, CodeElementKind, CodeElement,\
                            MethodElement
from codebase.actions import start_eclipse, stop_eclipse, check_eclipse,\
                             create_code_db, create_code_local, list_code_db,\
                             list_code_local, link_eclipse, get_codebase_path,\
                             create_code_element_kinds, parse_code
from project.models import Project
from project.actions import create_project_local, create_project_db,\
                            create_release_db


class EclipseTest(TestCase):

    @unittest.skip('Usually works.')
    def testEclipse(self):
        start_eclipse()
        self.assertTrue(check_eclipse())
        stop_eclipse()


class CodeSetup(TestCase):

    @classmethod
    def setUpClass(cls):
        time.sleep(1)
        start_eclipse()

    @classmethod
    def tearDownClass(cls):
        stop_eclipse()

    def setUp(self):
        settings.PROJECT_FS_ROOT = settings.PROJECT_FS_ROOT_TEST
        create_project_local('project1')
        create_project_db('Project 1', 'http://www.example1.com', 'project1')
        create_release_db('project1', '3.0', True)
        create_release_db('project1', '3.1')

    def tearDown(self):
        Project.objects.all().delete()
        CodeElementKind.objects.all().delete()
        clean_test_dir()

    def testCreateCodeDB(self):
        create_code_db('project1', 'core', '3.0')
        self.assertEqual(1, CodeBase.objects.all().count())

    def testCreateCodeLocal(self):
        create_code_local('project1', 'core', '3.0')
        create_code_local('project1', 'lib', '3.1')
        path = get_codebase_path('project1', root=True)
        self.assertEqual(2, len(os.listdir(path)))

    def testListCodeLocal(self):
        self.assertEqual(0, len(list_code_local('project1')))
        create_code_local('project1', 'core', '3.0')
        self.assertEqual(1, len(list_code_local('project1')))
        create_code_local('project1', 'lib', '3.0')
        create_code_local('project1', 'core', '3.1')
        self.assertEqual(3, len(list_code_local('project1')))

    def testListCodeDB(self):
        self.assertEqual(0, len(list_code_db('project1')))
        create_code_db('project1', 'core', '3.0')
        self.assertEqual(1, len(list_code_db('project1')))
        create_code_db('project1', 'lib', '3.0')
        create_code_db('project1', 'core', '3.1')
        self.assertEqual(3, len(list_code_db('project1')))

    def testCreateCodeElementKinds(self):
        create_code_element_kinds()
        kind_count = CodeElementKind.objects.all().count()
        self.assertEqual(30, kind_count)

    def testLinkEclipseProject(self):
        create_code_local('project1', 'core', '3.0')
        to_path = get_codebase_path('project1', 'core', '3.0')
        to_path = os.path.join(to_path, 'src')
        os.rmdir(to_path)
        from_path = os.path.join(settings.TESTDATA, 'testproject1', 'src')
        shutil.copytree(from_path, to_path)
        link_eclipse('project1', 'core', '3.0')

        gateway = JavaGateway()
        workspace = gateway.jvm.org.eclipse.core.resources.ResourcesPlugin.\
                getWorkspace()
        root = workspace.getRoot()
        pm = gateway.jvm.org.eclipse.core.runtime.NullProgressMonitor()
        project1 = root.getProject('project1core3.0')
        self.assertIsNotNone(project1)
        project1.delete(True, True, pm)
        time.sleep(1)
        gateway.close()


class CodeParserTest(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        time.sleep(1)
        settings.PROJECT_FS_ROOT = settings.PROJECT_FS_ROOT_TEST
        start_eclipse()
        create_project_local('project1')
        create_code_local('project1', 'core', '3.0')
        to_path = get_codebase_path('project1', 'core', '3.0')
        to_path = os.path.join(to_path, 'src')
        os.rmdir(to_path)
        from_path = os.path.join(settings.TESTDATA, 'testproject1', 'src')
        shutil.copytree(from_path, to_path)
        link_eclipse('project1', 'core', '3.0')

    @classmethod
    def tearDownClass(cls):
        gateway = JavaGateway()
        workspace = gateway.jvm.org.eclipse.core.resources.ResourcesPlugin.\
                getWorkspace()
        root = workspace.getRoot()
        pm = gateway.jvm.org.eclipse.core.runtime.NullProgressMonitor()
        project1 = root.getProject('project1core3.0')
        project1.delete(True, True, pm)
        time.sleep(1)
        gateway.close()
        stop_eclipse()
        clean_test_dir()

    @transaction.commit_on_success
    def setUp(self):
        create_code_element_kinds()
        create_project_db('Project 1', 'http://www.example1.com', 'project1')
        create_release_db('project1', '3.0', True)
        create_release_db('project1', '3.1')

    @transaction.commit_on_success
    def tearDown(self):
        Project.objects.all().delete()
        CodeElementKind.objects.all().delete()

    @transaction.autocommit
    def testJavaCodeParser(self):
        create_code_db('project1', 'core', '3.0')

        codebase = parse_code('project1', 'core', '3.0', 'java')

        ### Test some Classes ###
        ce = CodeElement.objects.get(fqn='RootApplication')
        self.assertEqual('RootApplication', ce.simple_name)
        self.assertEqual('class', ce.kind.kind)
        # Test containees & containers
        self.assertEqual(1, ce.containees.count())
        self.assertEqual('package', ce.containers.all()[0].kind.kind)
        self.assertEqual('', ce.containers.all()[0].fqn)

        ce = CodeElement.objects.get(fqn='p1.Application')
        self.assertEqual('Application', ce.simple_name)
        self.assertEqual('class', ce.kind.kind)
        # Test containees & containers
        self.assertEqual(1, ce.containees.count())
        self.assertEqual('package', ce.containers.all()[0].kind.kind)
        self.assertEqual('p1', ce.containers.all()[0].fqn)

        self.assertEqual(2,
                CodeElement.objects.filter(simple_name='Application').count())

        # Test hierarchy 
        ce = CodeElement.objects.get(fqn='p1.AnimalException')
        # Nothing because the parent is not in the codebase
        # (java.lang.Exception)
        self.assertEqual(0, ce.parents.count())

        ce = CodeElement.objects.get(fqn='p1.p2.Dog')
        fqns = [parent.fqn for parent in ce.parents.all()]
        self.assertTrue('p1.p2.Canidae' in fqns)
        self.assertTrue('p1.p2.Tag' in fqns)
        self.assertTrue('p1.p2.Tag2' in fqns)

        ### Test some Methods and Parameters ###
        ce = CodeElement.objects.get(fqn='p1.BigCat.doSomething')
        method = ce.methodelement
        self.assertEqual(4, method.parameters_length)
        self.assertTrue(MethodElement.objects.filter(simple_name='doSomething')
                .filter(parameters_length=4).exists())
        self.assertEqual(4, ce.parameters().count())
        self.assertEqual('method', ce.kind.kind)
        # Test container
        self.assertEqual('p1.BigCat', ce.containers.all()[0].fqn)
        
        self.assertEqual('specials', ce.parameters().all()[3].simple_name)
        # Array info is stripped from type.
        self.assertEqual('byte', ce.parameters().all()[2].type_fqn)
        # Generic info is stripped from type
        self.assertEqual('java.util.List', ce.parameters().all()[3].type_fqn)
        self.assertEqual('method parameter', ce.parameters().all()[3].kind.kind)

        ce = CodeElement.objects.get(fqn='p1.Animal.getParents')
        self.assertEqual('java.util.Collection', ce.methodelement.return_fqn)
        self.assertEqual('method', ce.kind.kind)
        # Test container
        self.assertEqual('p1.Animal', ce.containers.all()[0].fqn)

        ce = CodeElement.objects.get(fqn='p1.Animal.run')
        self.assertEqual('void', ce.methodelement.return_fqn)
        self.assertEqual('method', ce.kind.kind)

        ### Test some Fields ###
        ce = CodeElement.objects.get(fqn='p1.Animal.MAX_AGE')
        self.assertEqual('MAX_AGE', ce.simple_name)
        self.assertEqual('int', ce.fieldelement.type_simple_name)
        self.assertEqual('int', ce.fieldelement.type_fqn)
        self.assertEqual('field', ce.kind.kind)
        # Test container
        self.assertEqual('p1.Animal', ce.containers.all()[0].fqn)

        ce = CodeElement.objects.get(fqn='p1.Cat.name')
        self.assertEqual('java.lang.String', ce.fieldelement.type_fqn)
        self.assertEqual('field', ce.kind.kind)
        # Test container
        self.assertEqual('p1.Cat', ce.containers.all()[0].fqn)

        ### Test some Enumerations ###

        ### Test some Annotations ###

        self.assertEqual(106, codebase.code_elements.count())
