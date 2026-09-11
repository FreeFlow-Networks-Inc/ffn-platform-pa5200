import unittest,tempfile
from pathlib import Path
from unittest.mock import Mock,patch
import ffn_bcm_service as service
class ServiceTests(unittest.TestCase):
    def test_fixed_unit_and_acknowledgement(self):
        observed={'revision':3,'operation_complete':True,'service':{'MainPID':'10'}}
        with tempfile.TemporaryDirectory() as temp,patch.object(service,'STATE',Path(temp)/'state'),patch.object(service,'status',return_value=observed),patch.object(service.subprocess,'run') as run:
            with self.assertRaises(ValueError):service.apply({'revision':3,'operation':'restart','acknowledge_link_outage':False})
            with self.assertRaises(ValueError):service.apply({'revision':2,'operation':'restart','acknowledge_link_outage':True})
            run.assert_not_called()
            result=service.apply({'revision':3,'operation':'restart','acknowledge_link_outage':True})
            self.assertEqual(result['activation'],'pending')
            self.assertEqual(run.call_args.args[0],['systemctl','--no-block','restart','ffn-bcmd.service'])
if __name__=='__main__':unittest.main()
