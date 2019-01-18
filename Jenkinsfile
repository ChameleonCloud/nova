pipeline {
  agent any
 
  options {
    copyArtifactPermission(projectNames: 'nova*')
  }

  stages {
    stage('package') {
      steps {
        dir('dist') {
          deleteDir()
        }
        sh 'python setup.py sdist'
        sh 'find dist -type f -exec cp {} dist/nova.tar.gz \\;'
        archiveArtifacts(artifacts: 'dist/nova.tar.gz', onlyIfSuccessful: true)
      }
    }
  }
}

