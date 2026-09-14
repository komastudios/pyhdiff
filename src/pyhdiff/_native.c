/* Thin CPython layer over the C bridge. Every codec call runs with the GIL
 * released; inputs are immutable bytes objects kept alive by the caller's
 * frame. Framing, hashing and error messages live in pyhdiff/__init__.py. */
#ifndef Py_LIMITED_API
#define Py_LIMITED_API 0x030C0000 /* CMake passes the same value */
#endif
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <stdlib.h>

#include "bridge.h"
#include "c_buffer.h"

static PyObject *native_error;

typedef struct {
    const unsigned char *data;
    size_t size;
} view;

static int bytes_view(PyObject *object, view *out) {
    char *data;
    Py_ssize_t size;
    if (PyBytes_AsStringAndSize(object, &data, &size) < 0)
        return 0;
    out->data = (const unsigned char *)data;
    out->size = (size_t)size;
    return 1;
}

/* A payload that starts at offset inside an envelope, without copying it. */
static int tail_view(PyObject *object, Py_ssize_t offset, view *out) {
    if (!bytes_view(object, out))
        return 0;
    if (offset < 0 || (size_t)offset > out->size) {
        PyErr_SetString(PyExc_ValueError, "offset outside the buffer");
        return 0;
    }
    out->data = phd_at(out->data, out->size, (size_t)offset,
                       out->size - (size_t)offset);
    out->size -= (size_t)offset;
    return 1;
}

static PyObject *finish(phd_result result) {
    PyObject *bytes;
    if (result.status != PHD_OK) {
        PyObject *code = PyLong_FromLong((long)result.status);
        if (code) {
            PyErr_SetObject(native_error, code);
            Py_DECREF(code);
        }
        return NULL;
    }
    bytes = PyBytes_FromStringAndSize((const char *)result.data,
                                      (Py_ssize_t)result.size);
    free(result.data);
    return bytes;
}

static PyObject *hdiff_encode(PyObject *self, PyObject *args) {
    PyObject *base_object, *target_object;
    view base, target;
    phd_hdiff_options o;
    phd_result result;
    (void)self;
    if (!PyArg_ParseTuple(args, "O!O!iiiiiii", &PyBytes_Type, &base_object,
                          &PyBytes_Type, &target_object, &o.match_score,
                          &o.level, &o.window_log, &o.fast_block_bytes,
                          &o.step_bytes, &o.content_size, &o.checksum) ||
        !bytes_view(base_object, &base) || !bytes_view(target_object, &target))
        return NULL;
    Py_BEGIN_ALLOW_THREADS
    result = phd_hdiff_encode(base.data, base.size, target.data, target.size, &o);
    Py_END_ALLOW_THREADS
    return finish(result);
}

static PyObject *zstd_encode(PyObject *self, PyObject *args) {
    PyObject *base_object, *target_object;
    view base, target;
    phd_zstd_options o;
    phd_result result;
    (void)self;
    if (!PyArg_ParseTuple(args, "O!O!iiiiiiiii", &PyBytes_Type, &base_object,
                          &PyBytes_Type, &target_object, &o.level,
                          &o.window_log, &o.ldm, &o.content_size, &o.checksum,
                          &o.ldm_hash_log, &o.ldm_min_match, &o.ldm_bucket_log,
                          &o.ldm_rate_log) ||
        !bytes_view(base_object, &base) || !bytes_view(target_object, &target))
        return NULL;
    Py_BEGIN_ALLOW_THREADS
    result = phd_zstd_encode(base.data, base.size, target.data, target.size, &o);
    Py_END_ALLOW_THREADS
    return finish(result);
}

static PyObject *apply(PyObject *args, int zstd) {
    PyObject *base_object, *payload_object;
    Py_ssize_t offset, target_size;
    view base, payload;
    phd_result result;
    if (!PyArg_ParseTuple(args, "O!O!nn", &PyBytes_Type, &base_object,
                          &PyBytes_Type, &payload_object, &offset,
                          &target_size) ||
        !bytes_view(base_object, &base) ||
        !tail_view(payload_object, offset, &payload))
        return NULL;
    if (target_size < 0) {
        PyErr_SetString(PyExc_ValueError, "negative target size");
        return NULL;
    }
    Py_BEGIN_ALLOW_THREADS
    result = zstd ? phd_zstd_apply(base.data, base.size, payload.data,
                                   payload.size, (size_t)target_size)
                  : phd_hdiff_apply(base.data, base.size, payload.data,
                                    payload.size, (size_t)target_size);
    Py_END_ALLOW_THREADS
    return finish(result);
}

static PyObject *hdiff_apply(PyObject *self, PyObject *args) {
    (void)self;
    return apply(args, 0);
}

static PyObject *zstd_apply(PyObject *self, PyObject *args) {
    (void)self;
    return apply(args, 1);
}

static PyObject *compress(PyObject *self, PyObject *args) {
    PyObject *data_object;
    int level, window_log, checksum;
    view data;
    phd_result result;
    (void)self;
    if (!PyArg_ParseTuple(args, "O!iii", &PyBytes_Type, &data_object, &level,
                          &window_log, &checksum) ||
        !bytes_view(data_object, &data))
        return NULL;
    Py_BEGIN_ALLOW_THREADS
    result = phd_zstd_compress(data.data, data.size, level, window_log, checksum);
    Py_END_ALLOW_THREADS
    return finish(result);
}

static PyObject *decompress(PyObject *self, PyObject *args) {
    PyObject *data_object;
    Py_ssize_t max_output;
    view data;
    phd_result result;
    (void)self;
    if (!PyArg_ParseTuple(args, "O!n", &PyBytes_Type, &data_object,
                          &max_output) ||
        !bytes_view(data_object, &data))
        return NULL;
    if (max_output < 0) {
        PyErr_SetString(PyExc_ValueError, "negative output bound");
        return NULL;
    }
    Py_BEGIN_ALLOW_THREADS
    result = phd_zstd_decompress(data.data, data.size, (size_t)max_output);
    Py_END_ALLOW_THREADS
    return finish(result);
}

static PyMethodDef methods[] = {
    {"hdiff_encode", hdiff_encode, METH_VARARGS, NULL},
    {"hdiff_apply", hdiff_apply, METH_VARARGS, NULL},
    {"zstd_encode", zstd_encode, METH_VARARGS, NULL},
    {"zstd_apply", zstd_apply, METH_VARARGS, NULL},
    {"compress", compress, METH_VARARGS, NULL},
    {"decompress", decompress, METH_VARARGS, NULL},
    {NULL, NULL, 0, NULL},
};

static struct PyModuleDef module = {
    PyModuleDef_HEAD_INIT, "pyhdiff._native", NULL, -1, methods,
    NULL, NULL, NULL, NULL,
};

PyMODINIT_FUNC PyInit__native(void) {
    PyObject *m = PyModule_Create(&module);
    if (!m)
        return NULL;
    native_error = PyErr_NewException("pyhdiff._native.Error", NULL, NULL);
    if (!native_error || PyModule_AddObjectRef(m, "Error", native_error) < 0 ||
        PyModule_AddIntConstant(m, "RAW_MAX", (long)PHD_RAW_MAX) < 0 ||
        PyModule_AddIntConstant(m, "PAYLOAD_MAX", (long)PHD_PAYLOAD_MAX) < 0 ||
        PyModule_AddIntConstant(m, "FRAME_MAX", (long)PHD_FRAME_MAX) < 0 ||
        PyModule_AddIntConstant(m, "STATUS_INVALID", PHD_INVALID) < 0 ||
        PyModule_AddIntConstant(m, "STATUS_LIMIT", PHD_LIMIT) < 0 ||
        PyModule_AddIntConstant(m, "STATUS_ALLOC", PHD_ALLOC) < 0 ||
        PyModule_AddIntConstant(m, "STATUS_EXCEPTION", PHD_EXCEPTION) < 0 ||
        PyModule_AddIntConstant(m, "STATUS_UNKNOWN", PHD_UNKNOWN) < 0 ||
        PyModule_AddIntConstant(m, "STATUS_OPTION", PHD_OPTION) < 0 ||
        PyModule_AddIntConstant(m, "STATUS_ZSTD_ERROR_BASE",
                                PHD_ZSTD_ERROR_BASE) < 0) {
        Py_XDECREF(native_error);
        Py_DECREF(m);
        return NULL;
    }
    return m;
}
