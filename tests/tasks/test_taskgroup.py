import pytest

from ploomber import DAG
from ploomber.tasks import PythonCallable, TaskGroup
from ploomber.products import File, SQLRelation


def touch(product, param):
    pass


def touch_a_b(product, a, b):
    pass


def test_from_params():
    dag = DAG()
    group = TaskGroup.from_params(PythonCallable,
                                  File,
                                  'file.txt', {'source': touch},
                                  dag,
                                  name='task_group',
                                  params_array=[{
                                      'param': 1
                                  }, {
                                      'param': 2
                                  }])

    assert len(group) == 2

    dag.render()

    assert dag['task_group0'].source.primitive is touch
    assert dag['task_group1'].source.primitive is touch
    assert str(dag['task_group0'].product) == 'file_0.txt'
    assert str(dag['task_group1'].product) == 'file_1.txt'


def test_from_grid():
    dag = DAG()
    group = TaskGroup.from_grid(PythonCallable,
                                File,
                                'file.txt', {
                                    'source': touch_a_b,
                                },
                                dag,
                                name='task_group',
                                grid={
                                    'a': [1, 2],
                                    'b': [3, 4]
                                })

    assert len(group) == 4


def test_metaproduct():
    dag = DAG()
    group = TaskGroup.from_params(PythonCallable,
                                  File, {
                                      'one': 'one.txt',
                                      'another': 'another.txt'
                                  }, {'source': touch},
                                  dag,
                                  name='task_group',
                                  params_array=[{
                                      'param': 1
                                  }, {
                                      'param': 2
                                  }])

    assert str(dag['task_group0'].product['one']) == 'one_0.txt'
    assert str(dag['task_group0'].product['another']) == 'another_0.txt'
    assert str(dag['task_group1'].product['one']) == 'one_1.txt'
    assert str(dag['task_group1'].product['another']) == 'another_1.txt'
    assert len(group) == 2


def test_sql_product():
    dag = DAG()
    TaskGroup.from_params(PythonCallable,
                          SQLRelation, ['schema', 'one', 'table'],
                          {'source': touch},
                          dag=dag,
                          name='task_group',
                          params_array=[{
                              'param': 1
                          }, {
                              'param': 2
                          }])

    id_ = dag['task_group0'].product
    assert (id_.schema, id_.name, id_.kind) == ('schema', 'one_0', 'table')
    id_ = dag['task_group1'].product
    assert (id_.schema, id_.name, id_.kind) == ('schema', 'one_1', 'table')


def test_sql_meta_product():
    dag = DAG()
    TaskGroup.from_params(PythonCallable,
                          SQLRelation, {
                              'one': ['schema', 'one', 'table'],
                              'another': ['another', 'view']
                          }, {'source': touch},
                          dag=dag,
                          name='task_group',
                          params_array=[{
                              'param': 1
                          }, {
                              'param': 2
                          }])

    id_ = dag['task_group0'].product['one']
    assert (id_.schema, id_.name, id_.kind) == ('schema', 'one_0', 'table')
    id_ = dag['task_group0'].product['another']
    assert (id_.schema, id_.name, id_.kind) == (None, 'another_0', 'view')
    id_ = dag['task_group1'].product['one']
    assert (id_.schema, id_.name, id_.kind) == ('schema', 'one_1', 'table')
    id_ = dag['task_group1'].product['another']
    assert (id_.schema, id_.name, id_.kind) == (None, 'another_1', 'view')


@pytest.mark.parametrize('key', ['dag', 'name', 'params'])
def test_error_if_non_permitted_key_in_task_kwargs(key):
    dag = DAG()

    with pytest.raises(KeyError) as excinfo:
        TaskGroup.from_params(PythonCallable,
                              File,
                              'file.txt', {key: None},
                              dag,
                              name='task_group',
                              params_array=[{
                                  'param': 1
                              }, {
                                  'param': 2
                              }])

    assert 'should not be part of task_kwargs' in str(excinfo.value)


def test_error_if_required_keys_not_in_task_kwargs():
    dag = DAG()

    with pytest.raises(KeyError) as excinfo:
        TaskGroup.from_params(PythonCallable,
                              File,
                              'file.txt',
                              dict(),
                              dag,
                              name='task_group',
                              params_array=[{
                                  'param': 1
                              }, {
                                  'param': 2
                              }])

    assert 'should be in task_kwargs' in str(excinfo.value)


def test_error_if_wrong_product_primitive():
    dag = DAG()

    with pytest.raises(NotImplementedError):
        TaskGroup.from_params(PythonCallable,
                              File,
                              b'wrong-type.txt', {'source': touch},
                              dag,
                              name='task_group',
                              params_array=[{
                                  'param': 1
                              }])
