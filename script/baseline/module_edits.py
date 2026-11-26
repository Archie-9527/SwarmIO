# SPDX-License-Identifier: GPL-2.0-only
"""Exact source transformations for isolated baseline modules."""

DSA2_EDITS = [('struct idxd_device *idxd = __dev_id_to_idxd(0);',
  'struct idxd_device *idxd = __dev_id_to_idxd(2);'),
 ('\tif (p->num_devs > DSA_MAX_DEVS) {',
  '\tif (p->num_devs != 1) {\n'
  '\t\tSWARMIO_ERROR("DSA2-only comparison requires dsa_num_devs=1\\n");\n'
  '\t\treturn -EINVAL;\n'
  '\t}\n'
  '\n'
  '\tif (p->num_devs > DSA_MAX_DEVS) {'),
 ('\tfor (dsa_id = 0;\n'
  '\t     dsa_id < DSA_SCAN_MAX_DEVS && num_enabled_devs < requested_devs;\n'
  '\t     dsa_id++) {',
  '\tfor (dsa_id = 2;\n'
  '\t     dsa_id < 3 && num_enabled_devs < requested_devs;\n'
  '\t     dsa_id++) {')]

WEIGHTED_EDITS = [('src/main.c',
  'module_param(num_sched_insts, uint, 0444);',
  'unsigned int qid_dsa0_qps = 128;\n'
  'module_param(qid_dsa0_qps, uint, 0444);\n'
  'MODULE_PARM_DESC(qid_dsa0_qps, "DSA0 QPs for the isolated 256-QP, 8-SU comparison");\n'
  '\n'
  'module_param(num_sched_insts, uint, 0444);'),
 ('src/main.c',
  '\tif (!cpus || !*cpus) {',
  '\tif (qid_dsa0_qps < 4 || qid_dsa0_qps > 252 || qid_dsa0_qps % 4) {\n'
  '\t\tSWARMIO_ERROR("qid_dsa0_qps must be a multiple of 4 in [4, 252]\\n");\n'
  '\t\treturn -EINVAL;\n'
  '\t}\n'
  '\n'
  '\tif (!cpus || !*cpus) {'),
 ('nvmev/admin.c',
  '#include "queue.h"',
  '#include "queue.h"\n'
  '\n'
  'extern unsigned int qid_dsa0_qps;\n'
  '\n'
  'static unsigned int comparison_qid_count(unsigned int total,\n'
  '                                          unsigned int units,\n'
  '                                          unsigned int id)\n'
  '{\n'
  '\tif (total == 256 && units == 8)\n'
  '\t\treturn id < 4 ? qid_dsa0_qps / 4 : (256 - qid_dsa0_qps) / 4;\n'
  '\treturn (total + units - 1 - id) / units;\n'
  '}'),
 ('nvmev/admin.c',
  'len = (vdev->num_sq + num_service_units - 1 - i) /\n\t\t\t      num_service_units;',
  'len = comparison_qid_count(vdev->num_sq, num_service_units, i);')]
